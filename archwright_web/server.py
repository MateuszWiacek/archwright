"""Uvicorn launcher for the web UI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def run_server(
    config_path: Optional[Path] = None,
    config_dir: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
    host: str = "127.0.0.1",
    port: int = 8471,
    reload: bool = False,
) -> None:
    import uvicorn

    from archwright_web.app import create_app

    app = create_app(
        config_path=config_path,
        config_dir=config_dir,
        inventory_path=inventory_path,
    )
    uvicorn.run(app, host=host, port=port)
