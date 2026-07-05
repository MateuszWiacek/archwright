"""Restore wizard: multi-step UI flow.

Routes are thin: parse form input, delegate to ``restore_orchestrator``,
render a template. All restore-domain logic lives in the service.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from archwright_web.app import templates
from archwright_web.services import config_registry, restore_orchestrator
from archwright_web.services.job_runner import runner
from archwright_web.services.restore_orchestrator import RestoreInputError

router = APIRouter()


async def _step1_with_error(request: Request, selection, error: str):
    """Render step 1 with an error and any archives that loaded successfully."""
    context = config_registry.template_context(selection)
    archives_view = await asyncio.to_thread(
        restore_orchestrator.list_archives, selection,
    )
    context.update({
        "archives": archives_view.archives,
        "config": archives_view.config,
        "error": error,
    })
    return templates.TemplateResponse(request, "restore/step1_choose.html", context)


@router.get("/")
async def restore_start(request: Request, job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)
    context = config_registry.template_context(selection)
    view = await asyncio.to_thread(restore_orchestrator.list_archives, selection)
    context.update({
        "archives": view.archives,
        "config": view.config,
        "error": view.error or selection.error,
    })
    return templates.TemplateResponse(request, "restore/step1_choose.html", context)


@router.post("/plan")
async def restore_plan(request: Request, archive: str = Form(""), job: str = ""):
    selection = config_registry.select_job(request.app.state.config_source, job)

    try:
        view = await asyncio.to_thread(
            restore_orchestrator.build_plan, selection, archive,
        )
    except ValueError as exc:
        return await _step1_with_error(request, selection, str(exc))

    context = config_registry.template_context(selection)
    context.update({
        "archive": archive,
        "plan": view.plan,
        "conflicts": view.conflicts,
        "prefixes": view.prefixes,
        "config": view.config,
        "restore_can_execute": True,
        "is_remote": view.is_remote,
    })
    return templates.TemplateResponse(request, "restore/step2_plan.html", context)


@router.post("/plan/filter")
async def restore_plan_filtered(request: Request, job: str = ""):
    form_data = await request.form()
    archive = str(form_data.get("archive", ""))
    selected = form_data.getlist("prefixes")

    selection = config_registry.select_job(request.app.state.config_source, job)
    context = config_registry.template_context(selection)

    try:
        view = await asyncio.to_thread(
            restore_orchestrator.build_plan,
            selection, archive,
            selected_prefixes=selected,
        )
    except RestoreInputError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    except ValueError as exc:
        context.update({"plan": [], "conflicts": [], "error": str(exc)})
        return templates.TemplateResponse(
            request, "restore/_plan_table.html", context,
        )

    context.update({
        "plan": view.plan,
        "conflicts": view.conflicts,
        "error": None,
    })
    return templates.TemplateResponse(request, "restore/_plan_table.html", context)


@router.post("/confirm")
async def restore_confirm(request: Request, job: str = ""):
    form_data = await request.form()
    archive = str(form_data.get("archive", ""))
    selected = form_data.getlist("prefixes")
    filter_applied = form_data.get("prefix_filter_applied") == "1"
    overwrite = form_data.get("overwrite") == "on"

    selection = config_registry.select_job(request.app.state.config_source, job)
    context = config_registry.template_context(selection)

    view = await asyncio.to_thread(
        restore_orchestrator.build_plan,
        selection,
        archive,
        selected_prefixes=selected if filter_applied else None,
    )

    context.update({
        "archive": archive,
        "plan": view.plan,
        "conflicts": view.conflicts,
        "prefixes": selected,
        "overwrite": overwrite,
        "config": view.config,
    })
    return templates.TemplateResponse(request, "restore/step3_confirm.html", context)


@router.post("/execute")
async def restore_execute(request: Request, job: str = ""):
    form_data = await request.form()
    archive = str(form_data.get("archive", ""))
    confirmation = str(form_data.get("confirmation", "")).strip()
    overwrite = form_data.get("overwrite") == "on"
    selected = form_data.getlist("prefixes")
    filter_applied = form_data.get("prefix_filter_applied") == "1"
    prefixes_arg = selected if filter_applied else None

    selection = config_registry.select_job(request.app.state.config_source, job)

    if confirmation != "RESTORE":
        # Re-render step 3 with an error.
        view = await asyncio.to_thread(
            restore_orchestrator.build_plan,
            selection, archive,
            selected_prefixes=prefixes_arg,
        )

        context = config_registry.template_context(selection)
        context.update({
            "archive": archive,
            "plan": view.plan,
            "conflicts": view.conflicts,
            "prefixes": selected,
            "overwrite": overwrite,
            "config": view.config,
            "error": "Type RESTORE to confirm.",
        })
        return templates.TemplateResponse(
            request, "restore/step3_confirm.html", context,
        )

    try:
        started = restore_orchestrator.start_execution(
            selection,
            archive,
            overwrite=overwrite,
            selected_prefixes=prefixes_arg,
        )
    except NotImplementedError as exc:
        return PlainTextResponse(str(exc), status_code=501)

    context = config_registry.template_context(selection)
    if not started:
        context.update({
            "error": "A job is already running. Wait for it to finish.",
            "job": runner.current,
        })
    else:
        context.update({"error": None, "job": runner.current})
    return templates.TemplateResponse(
        request, "restore/step4_result.html", context,
    )
