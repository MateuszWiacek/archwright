"""Config editor: view, edit, validate, export YAML."""

from __future__ import annotations

from typing import Any, Dict

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import Response

from archwright_web.app import templates
from archwright_web.services import config_registry, config_service

router = APIRouter()


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


def _form_value(form_data, key: str, default: str = "") -> str:
    value = form_data.get(key, default)
    return str(value) if value is not None else default


def _form_to_yaml_data(form_data) -> Dict[str, Any]:
    backup_name = _form_value(form_data, "backup_name")
    target_base_dir = _form_value(form_data, "target_base_dir")
    keep_last = _safe_int(form_data.get("keep_last"), 0)
    log_level = _form_value(form_data, "log_level", "INFO")
    hook_timeout = _safe_int(form_data.get("hook_timeout"), 300)
    dump_timeout = _safe_int(form_data.get("dump_timeout"), 3600)

    structure: Dict[str, Dict[str, Any]] = {}
    databases: Dict[str, Dict[str, Any]] = {}

    idx = 0
    while f"sf_{idx}_folder" in form_data:
        folder = _form_value(form_data, f"sf_{idx}_folder")
        subfolder = _form_value(form_data, f"sf_{idx}_subfolder")
        source_dir = _form_value(form_data, f"sf_{idx}_source_dir")
        include = _form_value(form_data, f"sf_{idx}_include")
        exclude = _form_value(form_data, f"sf_{idx}_exclude")
        pre_cmd = _form_value(form_data, f"sf_{idx}_pre_command")
        post_cmd = _form_value(form_data, f"sf_{idx}_post_command")

        if folder and subfolder:
            entry: Dict[str, Any] = {"source_dir": source_dir, "include": include}
            if exclude:
                entry["exclude"] = exclude
            if pre_cmd:
                entry["pre_command"] = pre_cmd
            if post_cmd:
                entry["post_command"] = post_cmd
            structure.setdefault(folder, {})[subfolder] = entry
        idx += 1

    idx = 0
    while f"db_{idx}_name" in form_data:
        name = _form_value(form_data, f"db_{idx}_name")
        provider = _form_value(form_data, f"db_{idx}_provider")
        if name and provider:
            db_entry: Dict[str, Any] = {"provider": provider}
            for key in (
                "dbname", "host", "port", "user", "password",
                "pg_dump_path", "container", "docker_path",
                "db_path", "sqlite3_path", "stop_command", "start_command",
            ):
                val = _form_value(form_data, f"db_{idx}_{key}")
                if val:
                    db_entry[key] = _safe_int(val, 5432) if key == "port" else val
            extra = _form_value(form_data, f"db_{idx}_extra_args")
            if extra:
                db_entry["extra_args"] = [
                    item.strip() for item in extra.split(",") if item.strip()
                ]
            databases[name] = db_entry
        idx += 1

    data: Dict[str, Any] = {
        "backup_name": backup_name,
        "target_base_dir": target_base_dir,
        "keep_last": keep_last,
        "structure": structure if structure else {
            "example": {"files": {"source_dir": "/tmp", "include": "*"}}  # nosec B108 - placeholder shown in the YAML preview for an empty form
        },
    }
    if log_level != "INFO":
        data["log_level"] = log_level
    if hook_timeout != 300:
        data["hook_timeout"] = hook_timeout
    if dump_timeout != 3600:
        data["dump_timeout"] = dump_timeout
    if databases:
        data["databases"] = databases
    return data


def _dump_yaml(data: Dict[str, Any]) -> str:
    return yaml.dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


@router.get("/")
async def editor(request: Request, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    context = config_registry.template_context(selection)
    error = selection.error
    config = None
    yaml_text = ""

    if selection.selected is not None and selection.selected.config is not None:
        config = selection.selected.config
        yaml_text = config_service.export_yaml(config)

    context.update({
        "config": config,
        "yaml_text": yaml_text,
        "error": error,
    })
    return templates.TemplateResponse(request, "config/editor.html", context)


@router.post("/preview-yaml")
async def preview_yaml(request: Request):
    """HTMX endpoint: re-render YAML from current form state."""
    form_data = await request.form()
    yaml_text = _dump_yaml(_form_to_yaml_data(form_data))

    return templates.TemplateResponse(request, "config/_yaml_preview.html", {
        "yaml_text": yaml_text,
    })


@router.get("/export")
async def export_yaml(request: Request, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    _, config = config_registry.require_config(selection)
    yaml_text = config_service.export_yaml(config)

    return Response(
        content=yaml_text,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=archwright-config.yaml"},
    )


@router.post("/export")
async def export_yaml_from_form(request: Request):
    """Export YAML from current form state (POST with form data)."""
    form_data = await request.form()
    yaml_text = _dump_yaml(_form_to_yaml_data(form_data))

    return Response(
        content=yaml_text,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=archwright-config.yaml"},
    )
