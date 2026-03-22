"""Logger setup for the backup tool."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from backup.constants import LOG_DATE_FORMAT, LOG_FORMAT


def setup_logging(
    log_file_path: Optional[Path] = None,
    dry_run: bool = False,
    console_level: str = "INFO",
) -> logging.Logger:
    """Configure and return the shared application logger."""
    logger = logging.getLogger("backup")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    if log_file_path and not dry_run:
        file_handler = logging.FileHandler(
            str(log_file_path), mode="w", encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
