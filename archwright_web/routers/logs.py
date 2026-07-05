"""Log file viewer."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse

from archwright_web.app import templates
from archwright_web.services import config_registry
from archwright_web.services.inventory import EXECUTOR_LOCAL
from archwright_web.services.safe_paths import safe_child_path

router = APIRouter()


def _safe_log_path(base_dir: Path, filename: str) -> Path:
    return safe_child_path(
        base_dir,
        filename,
        label="log filename",
        required_suffix=".log",
    )


@router.get("/{filename}")
async def view_log(request: Request, filename: str, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    if (
        selection.selected is not None
        and selection.selected.executor != EXECUTOR_LOCAL
    ):
        return PlainTextResponse(
            "Remote log viewing is not wired yet.",
            status_code=501,
        )
    try:
        _, config = config_registry.require_config(selection)
    except ValueError as exc:
        return PlainTextResponse(f"Error: {exc}", status_code=400)

    try:
        log_path = _safe_log_path(config.target_base_dir, filename)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    if not log_path.is_file():
        return PlainTextResponse("Log not found", status_code=404)

    content = log_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    context = config_registry.template_context(selection)
    context.update({
        "filename": filename,
        "lines": lines,
    })
    return templates.TemplateResponse(request, "logs/view.html", context)


@router.get("/{filename}/raw")
async def download_log(request: Request, filename: str, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    if (
        selection.selected is not None
        and selection.selected.executor != EXECUTOR_LOCAL
    ):
        return PlainTextResponse(
            "Remote log download is not wired yet.",
            status_code=501,
        )
    try:
        _, config = config_registry.require_config(selection)
    except ValueError as exc:
        return PlainTextResponse(f"Error: {exc}", status_code=400)

    try:
        log_path = _safe_log_path(config.target_base_dir, filename)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    if not log_path.is_file():
        return PlainTextResponse("Log not found", status_code=404)

    return FileResponse(
        path=str(log_path),
        filename=filename,
        media_type="text/plain",
    )
