"""Glob preview endpoint for live file matching."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request

from archwright_web.app import templates
from archwright_web.services.collector_service import preview_glob

router = APIRouter()


@router.post("/glob")
async def glob_preview(
    request: Request,
    source_dir: str = Form(""),
    include: str = Form("*"),
    exclude: str = Form(""),
):
    if not source_dir.strip() or not include.strip():
        return templates.TemplateResponse(request, "config/_preview_results.html", {
            "files": [],
            "total_count": 0,
            "truncated": False,
            "error": None,
        })

    result = preview_glob(
        source_dir=source_dir.strip(),
        include=include.strip(),
        exclude=exclude.strip() or None,
    )

    return templates.TemplateResponse(request, "config/_preview_results.html", {
        "files": result.files,
        "total_count": result.total_count,
        "truncated": result.truncated,
        "error": result.error,
    })
