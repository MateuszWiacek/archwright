"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from archwright_web.services.config_registry import create_source

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


async def _value_error_handler(request: Request, exc: ValueError) -> PlainTextResponse:
    """Map uncaught ``ValueError`` to HTTP 400 with the message.

    archwright services raise ``ValueError`` for user-facing input errors
    (unsafe paths, missing config, malformed archive names). Routes can
    still catch them explicitly when they need to render a template or
    use a different status code; this handler covers the common case of
    "just surface the message as 400" so individual routes do not need
    a try/except wrapper around every validating call.
    """
    return PlainTextResponse(str(exc), status_code=400)


def create_app(
    config_path: Optional[Path] = None,
    *,
    config_dir: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
) -> FastAPI:
    app = FastAPI(title="Archwright Web UI", docs_url=None, redoc_url=None)

    app.state.config_source = create_source(
        config_path,
        config_dir,
        inventory_path,
    )
    app.state.config_path = app.state.config_source.config_path

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.add_exception_handler(ValueError, _value_error_handler)

    from archwright_web.routers import archives, config_editor, dashboard, jobs, logs, preview, restore

    app.include_router(dashboard.router)
    app.include_router(config_editor.router, prefix="/config")
    app.include_router(preview.router, prefix="/preview")
    app.include_router(archives.router, prefix="/archives")
    app.include_router(logs.router, prefix="/logs")
    app.include_router(jobs.router, prefix="/jobs")
    app.include_router(restore.router, prefix="/restore")

    return app
