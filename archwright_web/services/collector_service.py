"""Glob preview service wrapping backup.collector."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from backup.collector import collect_files
from backup.models import BackupConfig, SubfolderConfig

MAX_PREVIEW_FILES = 500


@dataclass
class PreviewResult:
    files: List[str]
    total_count: int
    truncated: bool
    error: Optional[str] = None


def preview_glob(
    source_dir: str,
    include: str,
    exclude: Optional[str] = None,
) -> PreviewResult:
    src = Path(source_dir)
    if not src.is_dir():
        return PreviewResult(
            files=[], total_count=0, truncated=False,
            error=f"Directory does not exist: {source_dir}",
        )

    config = BackupConfig(
        backup_name="__preview__",
        target_base_dir=Path("/tmp"),  # nosec B108 - dummy value; preview never writes here
        keep_last=0,
        subfolders=[
            SubfolderConfig(
                folder_name="preview",
                subfolder_name="preview",
                source_dir=src,
                include=include,
                exclude=exclude,
            )
        ],
    )

    logger = logging.getLogger("archwright.preview")
    logger.setLevel(logging.WARNING)

    try:
        collected = collect_files(config, logger)
    except ValueError as exc:
        return PreviewResult(files=[], total_count=0, truncated=False, error=str(exc))

    paths = [str(cf.source_path) for cf in collected]
    total = len(paths)
    truncated = total > MAX_PREVIEW_FILES

    return PreviewResult(
        files=paths[:MAX_PREVIEW_FILES],
        total_count=total,
        truncated=truncated,
    )
