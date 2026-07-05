"""Dashboard / landing page."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import APIRouter, Request

from archwright_web.app import templates
from archwright_web.services import archive_service, config_registry
from archwright_web.services.job_runner import runner

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    context = config_registry.template_context(selection)
    error = selection.error
    archives = []
    config = None

    if selection.selected is not None and not selection.error:
        job = selection.selected
        executor = config_registry.executor_for_job(job)
        try:
            listing = await asyncio.to_thread(
                archive_service.read_archive_listing, job.path, executor,
            )
            archives = listing.archives
            config = job.config or SimpleNamespace(
                backup_name=listing.backup_name or job.id,
                target_base_dir=listing.target_base_dir,
                keep_last=listing.keep_last,
                subfolders=[],
                databases=[],
            )
        except archive_service.ArchiveListError as exc:
            error = str(exc)

    context.update({
        "config": config,
        "archives": archives,
        "error": error,
        "job": runner.current,
    })
    return templates.TemplateResponse(request, "dashboard.html", context)
