"""Job execution: trigger backup/validate, poll status."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Request

from archwright_web.app import templates
from archwright_web.services import config_registry
from archwright_web.services.executor import JsonCommandResult
from archwright_web.services.inventory import EXECUTOR_LOCAL
from archwright_web.services.job_runner import JobResult, JobStatus, runner

router = APIRouter()


def _remote_job_result(action: str, result: JsonCommandResult) -> JobResult:
    now = datetime.now()
    payload_error = result.payload.get("error")
    return JobResult(
        status=JobStatus.SUCCESS if result.ok else JobStatus.FAILED,
        started_at=now,
        finished_at=now,
        action=action,
        exit_code=result.exit_code,
        log_lines=json.dumps(result.payload, indent=2).splitlines(),
        error=str(payload_error) if payload_error else None,
    )


def _render_status(request: Request, selection, *, job: JobResult, error: Optional[str]):
    context = config_registry.template_context(selection)
    context.update({"job": job, "error": error})
    return templates.TemplateResponse(
        request, "partials/_job_status.html", context,
    )


async def _trigger(
    request: Request,
    job_param: str,
    action_label: str,
    *,
    local_start: Callable[[Path], bool],
    remote_async: Optional[Callable[[object], bool]] = None,
    remote_sync: Optional[Callable[[object], JsonCommandResult]] = None,
):
    """Common dispatch for trigger endpoints.

    ``local_start(config_path)`` runs a local job in the background.
    ``remote_async(selected_job)`` spawns a streaming remote job in the
    background (used for live backup). ``remote_sync(selected_job)``
    runs a synchronous remote action and returns its result for display
    (used for dry-run and validate). Exactly one of the remote callbacks
    must be provided.

    ``remote_sync`` blocks on an SSH call, so we offload it to a worker
    thread to keep the event loop responsive while it runs. The other
    paths already return immediately because they spawn a background
    thread through ``JobRunner``.
    """
    if (remote_async is None) == (remote_sync is None):
        raise ValueError(
            "Exactly one of remote_async or remote_sync must be provided."
        )

    selection = config_registry.select_job(request.app.state.config_source, job_param)

    try:
        selected_job = config_registry.require_job(selection)
    except ValueError as exc:
        return _render_status(
            request, selection, job=runner.current, error=str(exc),
        )

    if selected_job.executor != EXECUTOR_LOCAL:
        if remote_async is not None:
            started = remote_async(selected_job)
            error = None if started else "A job is already running."
            return _render_status(
                request, selection, job=runner.current, error=error,
            )
        assert remote_sync is not None
        result = await asyncio.to_thread(remote_sync, selected_job)
        return _render_status(
            request,
            selection,
            job=_remote_job_result(action_label, result),
            error=None,
        )

    config_path, _ = config_registry.require_config(selection)
    started = local_start(config_path)
    error = None if started else "A job is already running."
    return _render_status(
        request, selection, job=runner.current, error=error,
    )


def _executor_for(selected_job):
    return config_registry.executor_for_job(selected_job)


@router.post("/backup")
async def trigger_backup(request: Request, job: str = ""):
    return await _trigger(
        request, job, "remote backup",
        local_start=lambda path: runner.run_backup(path),
        remote_async=lambda sj: runner.run_remote_backup(
            lambda on_line: _executor_for(sj).backup(sj.path, on_line=on_line),
        ),
    )


@router.post("/backup/dry-run")
async def trigger_dry_run(request: Request, job: str = ""):
    return await _trigger(
        request, job, "remote dry-run backup",
        local_start=lambda path: runner.run_backup(path, dry_run=True),
        remote_sync=lambda sj: _executor_for(sj).backup_dry_run(sj.path),
    )


@router.post("/validate")
async def trigger_validate(request: Request, job: str = ""):
    return await _trigger(
        request, job, "remote validate",
        local_start=lambda path: runner.run_validate(path),
        remote_sync=lambda sj: _executor_for(sj).validate(sj.path),
    )


@router.get("/status")
async def job_status(request: Request, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    return _render_status(request, selection, job=runner.current, error=None)


@router.get("/logs")
async def job_logs(request: Request, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    context = config_registry.template_context(selection)
    context.update({"job": runner.current})
    return templates.TemplateResponse(
        request, "partials/_job_logs.html", context,
    )
