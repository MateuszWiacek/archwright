"""Archive browser: list, inspect, download."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

from archwright_web.app import templates
from archwright_web.services import archive_service, config_registry
from archwright_web.services.inventory import EXECUTOR_LOCAL
from archwright_web.services.safe_paths import safe_child_path

router = APIRouter()


def _validate_filename(filename: str) -> None:
    """Reject path traversal in archive filenames."""
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or "\x00" in filename
    ):
        raise ValueError(f"Invalid filename: {filename}")


def _safe_archive_path(base_dir: Path, filename: str) -> Path:
    return safe_child_path(
        base_dir,
        filename,
        label="archive filename",
        required_suffix=".zip",
    )


def _attachment_header(filename: str) -> str:
    ascii_name = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\", ";"} else "_"
        for char in filename
    ).strip()
    if not ascii_name:
        ascii_name = "download"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _remote_not_wired(action: str) -> PlainTextResponse:
    return PlainTextResponse(
        f"Remote archive {action} is not wired yet.",
        status_code=501,
    )


def _is_remote(selection) -> bool:
    return (
        selection.selected is not None
        and selection.selected.executor != EXECUTOR_LOCAL
    )


@router.get("/")
async def list_archives(request: Request, job: str = ""):
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
            )
        except archive_service.ArchiveListError as exc:
            error = str(exc)

    context.update({
        "archives": archives,
        "config": config,
        "error": error,
    })
    return templates.TemplateResponse(request, "archives/list.html", context)


@router.get("/{filename}/contents")
async def archive_contents(request: Request, filename: str, job: str = ""):
    _validate_filename(filename)
    selection = config_registry.select_job(request.app.state.config_source, job)
    if _is_remote(selection):
        return _remote_not_wired("inspection")

    _, config = config_registry.require_config(selection)
    zip_path = _safe_archive_path(config.target_base_dir, filename)
    entries = archive_service.list_zip_contents(zip_path)

    context = config_registry.template_context(selection)
    context.update({
        "filename": filename,
        "entries": entries,
    })
    return templates.TemplateResponse(request, "archives/_contents.html", context)


@router.get("/{filename}/download")
async def download_archive(request: Request, filename: str, job: str = ""):
    _validate_filename(filename)
    selection = config_registry.select_job(request.app.state.config_source, job)
    if _is_remote(selection):
        return _remote_not_wired("download")

    _, config = config_registry.require_config(selection)
    zip_path = _safe_archive_path(config.target_base_dir, filename)
    if not zip_path.is_file():
        return PlainTextResponse("Archive not found", status_code=404)

    return FileResponse(
        path=str(zip_path),
        filename=filename,
        media_type="application/zip",
    )


@router.get("/{filename}/entry")
async def view_entry(request: Request, filename: str, path: str = "", job: str = ""):
    _validate_filename(filename)
    selection = config_registry.select_job(request.app.state.config_source, job)
    if _is_remote(selection):
        return _remote_not_wired("ZIP entry inspection")

    _, config = config_registry.require_config(selection)
    zip_path = _safe_archive_path(config.target_base_dir, filename)
    entry_size = archive_service.get_zip_entry_size(zip_path, path)
    if entry_size is None:
        return PlainTextResponse("Entry not found", status_code=404)

    safe_name = path.split("/")[-1]

    if entry_size > archive_service.MAX_INLINE_SIZE:
        stream = archive_service.stream_zip_entry(zip_path, path)
        if stream is None:
            return PlainTextResponse("Entry not found", status_code=404)
        return StreamingResponse(
            stream,
            media_type="application/octet-stream",
            headers={"Content-Disposition": _attachment_header(safe_name)},
        )

    stream = archive_service.stream_zip_entry(zip_path, path)
    if stream is None:
        return PlainTextResponse("Entry not found", status_code=404)
    data = b"".join(stream)

    try:
        text = data.decode("utf-8")
        return PlainTextResponse(text)
    except UnicodeDecodeError:
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": _attachment_header(safe_name)},
        )
