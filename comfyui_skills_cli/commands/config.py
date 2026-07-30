"""comfyui-skill config export / import — configuration transfer."""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import typer

from comfyui_mcp_skills.domain.identifiers import validate_identifier
from comfyui_mcp_skills.domain.workflow_schema import (
    normalize_parameters,
    validate_parameter_targets,
)
from comfyui_mcp_skills.infrastructure.persistence.migration_lock import (
    project_migration_lock,
)

from ..config import get_base_dir, get_servers, load_config, save_config
from ..output import output_error, output_event, output_result
from ..storage import _safe_path

app = typer.Typer()


_DEFAULT_EXPORT_NAME = "comfyui-skill-export.json"


@app.command("export")
def config_export(
    ctx: typer.Context,
    output: str = typer.Option(
        "", "--output", "-o", help="Output file or directory (default: ./comfyui-skill-export.json)"
    ),
    portable_only: bool = typer.Option(
        True,
        "--portable-only/--include-secrets",
        help="Exclude server URLs and credentials by default",
    ),
):
    """Export config and workflows as a portable bundle."""
    if not output:
        output = os.path.join(os.getcwd(), _DEFAULT_EXPORT_NAME)
    elif os.path.isdir(output):
        output = os.path.join(output, _DEFAULT_EXPORT_NAME)
    else:
        output = os.path.abspath(output)

    base_dir = get_base_dir(ctx.obj.get("base_dir", ""))
    config = load_config(base_dir)

    bundle: dict[str, Any] = {
        "version": 1,
        "config": {},
        "workflows": {},
    }

    # Config
    servers = []
    for s in get_servers(config):
        server_entry: dict[str, Any] = {
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "enabled": s.get("enabled", True),
            "output_dir": s.get("output_dir", "./outputs"),
        }
        if not portable_only:
            server_entry["url"] = s.get("url", "")
            if s.get("auth"):
                server_entry["auth"] = s["auth"]
            if s.get("comfy_api_key"):
                server_entry["comfy_api_key"] = s["comfy_api_key"]
        servers.append(server_entry)

    bundle["config"] = {
        "servers": servers,
        "default_server": config.get("default_server", ""),
    }

    # Workflows
    data_dir = base_dir / "data"
    if data_dir.exists():
        for server_dir in sorted(data_dir.iterdir()):
            if not server_dir.is_dir():
                continue
            server_id = server_dir.name
            for workflow_dir in sorted(server_dir.iterdir()):
                if not workflow_dir.is_dir():
                    continue
                workflow_id = workflow_dir.name
                entry: dict[str, Any] = {}

                workflow_path = workflow_dir / "workflow.json"
                schema_path = workflow_dir / "schema.json"

                if workflow_path.exists():
                    with open(workflow_path, encoding="utf-8") as f:
                        entry["workflow"] = json.load(f)
                if schema_path.exists():
                    with open(schema_path, encoding="utf-8") as f:
                        entry["schema"] = json.load(f)

                if entry:
                    bundle["workflows"][f"{server_id}/{workflow_id}"] = entry

    with open(output, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)

    output_result(
        ctx,
        {
            "exported": output,
            "servers": len(servers),
            "workflows": len(bundle["workflows"]),
        },
    )


@app.command("import")
def config_import(
    ctx: typer.Context,
    input_path: str = typer.Argument(help="Path to bundle JSON file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without applying"),
    apply_environment: bool = typer.Option(
        False, "--apply-environment", help="Also apply server URLs and default_server from bundle"
    ),
    no_overwrite: bool = typer.Option(False, "--no-overwrite", help="Skip existing workflows"),
):
    """Import config and workflows from a bundle."""
    if not os.path.isfile(input_path):
        output_error(ctx, "FILE_NOT_FOUND", f'Bundle file not found: "{input_path}"')
        return

    with open(input_path, encoding="utf-8") as f:
        bundle = json.load(f)

    if not isinstance(bundle, dict) or "config" not in bundle:
        output_error(ctx, "INVALID_BUNDLE", "Invalid bundle format.")
        return

    base_dir = get_base_dir(ctx.obj.get("base_dir", ""))
    config = load_config(base_dir)
    original_config = copy.deepcopy(config)

    bundle_config = bundle.get("config", {})
    if not isinstance(bundle_config, dict):
        output_error(ctx, "INVALID_BUNDLE", "Bundle config must be an object.")
        return
    bundle_workflows = bundle.get("workflows", {})
    bundle_servers = bundle_config.get("servers", [])
    if not isinstance(bundle_servers, list):
        output_error(ctx, "INVALID_BUNDLE", "Bundle servers must be an array.")
        return
    validated_servers: list[dict[str, Any]] = []
    for server in bundle_servers:
        if not isinstance(server, dict):
            output_error(ctx, "INVALID_BUNDLE", "Bundle server must be an object.")
            return
        try:
            validate_identifier(server.get("id"), field="server_id")
        except ValueError as exc:
            output_error(ctx, "INVALID_BUNDLE", str(exc))
            return
        if "enabled" in server and not isinstance(server["enabled"], bool):
            output_error(ctx, "INVALID_BUNDLE", "Server enabled must be a JSON boolean.")
            return
        validated_servers.append(server)
    bundle_servers = validated_servers

    if not isinstance(bundle_workflows, dict):
        output_error(ctx, "INVALID_BUNDLE", "Bundle workflows must be an object.")
        return

    validated_workflows: dict[str, tuple[str, str, Path]] = {}
    for wf_key in bundle_workflows:
        parts = wf_key.split("/", 1) if isinstance(wf_key, str) else []
        if len(parts) != 2 or not all(parts):
            output_error(ctx, "INVALID_BUNDLE", f'Invalid workflow ID: "{wf_key}"')
            return
        server_id, workflow_id = parts
        try:
            workflow_dir = _safe_path(base_dir, server_id, workflow_id)
        except ValueError:
            output_error(ctx, "INVALID_BUNDLE", f'Unsafe workflow ID: "{wf_key}"')
            return
        validated_workflows[wf_key] = (server_id, workflow_id, workflow_dir)
        wf_data = bundle_workflows[wf_key]
        if not isinstance(wf_data, dict):
            output_error(ctx, "INVALID_BUNDLE", f'Workflow "{wf_key}" must be an object.')
            return
        if not isinstance(wf_data.get("workflow"), dict) or not isinstance(
            wf_data.get("schema"), dict
        ):
            output_error(
                ctx,
                "INVALID_BUNDLE",
                f'Workflow "{wf_key}" requires object workflow and schema.',
            )
            return
        workflow_schema = wf_data["schema"]
        if "enabled" in workflow_schema and not isinstance(workflow_schema["enabled"], bool):
            output_error(
                ctx,
                "INVALID_BUNDLE",
                f'Workflow "{wf_key}" enabled must be a JSON boolean.',
            )
            return
        try:
            parameters = normalize_parameters(workflow_schema)
            validate_parameter_targets(parameters, wf_data["workflow"])
        except ValueError as exc:
            output_error(
                ctx,
                "INVALID_BUNDLE",
                f'Workflow "{wf_key}" schema is invalid: {exc}',
            )
            return

    # Preview
    preview: dict[str, Any] = {
        "servers_in_bundle": len(bundle_servers),
        "workflows_in_bundle": len(bundle_workflows),
        "actions": [],
    }

    # Server merge plan
    existing_ids = {s.get("id") for s in get_servers(config)}
    for bs in bundle_servers:
        sid = bs.get("id", "")
        if sid in existing_ids:
            preview["actions"].append({"type": "server", "id": sid, "action": "merge"})
        else:
            preview["actions"].append({"type": "server", "id": sid, "action": "add"})

    # Workflow plan
    for wf_key, (_server_id, _workflow_id, workflow_dir) in validated_workflows.items():
        if workflow_dir.exists():
            if no_overwrite:
                preview["actions"].append({"type": "workflow", "id": wf_key, "action": "skip"})
            else:
                preview["actions"].append({"type": "workflow", "id": wf_key, "action": "overwrite"})
        else:
            preview["actions"].append({"type": "workflow", "id": wf_key, "action": "create"})

    if dry_run:
        output_result(ctx, preview)
        return

    # Apply servers
    for bs in bundle_servers:
        sid = bs.get("id", "")
        existing = None
        for s in config.get("servers", []):
            if s.get("id") == sid:
                existing = s
                break

        if existing:
            # Merge: update name, enabled; optionally URL
            existing["name"] = bs.get("name", existing.get("name", ""))
            existing["enabled"] = bs.get("enabled", existing.get("enabled", True))
            if apply_environment:
                incoming_url = bs.get("url")
                if incoming_url is not None and incoming_url != existing.get("url"):
                    existing.pop("auth", None)
                    existing.pop("comfy_api_key", None)
                if "url" in bs:
                    existing["url"] = bs["url"]
                if "auth" in bs:
                    existing["auth"] = bs["auth"]
                if "comfy_api_key" in bs:
                    existing["comfy_api_key"] = bs["comfy_api_key"]
        else:
            added = {
                "id": sid,
                "name": bs.get("name", sid),
                "enabled": bs.get("enabled", True),
            }
            if apply_environment:
                for field in ("url", "auth", "comfy_api_key", "output_dir"):
                    if field in bs:
                        added[field] = bs[field]
            config.setdefault("servers", []).append(added)

    if apply_environment and "default_server" in bundle_config:
        try:
            validate_identifier(bundle_config["default_server"], field="default_server")
        except ValueError as exc:
            output_error(ctx, "INVALID_BUNDLE", str(exc))
            return
    if apply_environment and "default_server" in bundle_config:
        config["default_server"] = bundle_config["default_server"]

    created = 0
    overwritten = 0
    skipped = 0
    staged: list[tuple[str, Path, Path, Path, bool]] = []
    migration_lock = project_migration_lock(base_dir)
    migration_lock.acquire()
    try:
        for wf_key, wf_data in bundle_workflows.items():
            _server_id, _workflow_id, workflow_dir = validated_workflows[wf_key]
            if workflow_dir.exists() and no_overwrite:
                skipped += 1
                continue
            stage = workflow_dir.with_name(f".{workflow_dir.name}.{uuid.uuid4().hex}.tmp")
            backup = workflow_dir.with_name(f".{workflow_dir.name}.{uuid.uuid4().hex}.backup")
            if workflow_dir.exists():
                shutil.copytree(workflow_dir, stage)
            else:
                stage.mkdir(parents=True)
            (stage / "workflow.json").write_text(
                json.dumps(wf_data["workflow"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (stage / "schema.json").write_text(
                json.dumps(wf_data["schema"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            staged.append((wf_key, workflow_dir, stage, backup, workflow_dir.exists()))

        save_config(base_dir, config)
        committed: list[tuple[Path, Path, bool]] = []
        for wf_key, workflow_dir, stage, backup, existed in staged:
            if existed:
                os.replace(workflow_dir, backup)
            try:
                os.replace(stage, workflow_dir)
            except Exception:
                if backup.exists():
                    os.replace(backup, workflow_dir)
                raise
            committed.append((workflow_dir, backup, existed))
            if existed:
                overwritten += 1
            else:
                created += 1
            output_event(ctx, "imported", workflow=wf_key)
    except Exception as exc:
        for workflow_dir, backup, existed in reversed(locals().get("committed", [])):
            shutil.rmtree(workflow_dir, ignore_errors=True)
            if existed and backup.exists():
                os.replace(backup, workflow_dir)
        save_config(base_dir, original_config)
        output_error(ctx, "IMPORT_FAILED", f"Import was rolled back: {exc}")
        return
    finally:
        for _wf_key, _workflow_dir, stage, backup, _existed in staged:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
        migration_lock.release()

    output_result(
        ctx,
        {
            "created": created,
            "overwritten": overwritten,
            "skipped": skipped,
            "servers_updated": len(bundle_servers),
        },
    )
