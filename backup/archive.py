"""ZIP archive creation with sanitized metadata."""

from __future__ import annotations

import logging
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

from backup.constants import (
    STREAM_CHUNK_SIZE,
    ZIP_COMPRESS_LEVEL,
    ZIP_COMPRESSION,
    ZIP_EXTERNAL_ATTR,
    ZIP_MAX_TIMESTAMP,
    ZIP_MIN_TIMESTAMP,
)
from backup.models import CollectedFile

def _make_clean_zipinfo(archive_path: str, source_path: Path) -> zipfile.ZipInfo:
    """Build ZipInfo with clamped timestamps and neutral permissions."""
    mod_time = datetime.fromtimestamp(source_path.stat().st_mtime)
    if mod_time < ZIP_MIN_TIMESTAMP:
        mod_time = ZIP_MIN_TIMESTAMP
    elif mod_time > ZIP_MAX_TIMESTAMP:
        mod_time = ZIP_MAX_TIMESTAMP
    info = zipfile.ZipInfo(
        filename=archive_path,
        date_time=(
            mod_time.year,
            mod_time.month,
            mod_time.day,
            mod_time.hour,
            mod_time.minute,
            mod_time.second,
        ),
    )
    info.compress_type = ZIP_COMPRESSION
    info.external_attr = ZIP_EXTERNAL_ATTR
    return info

def create_archive(
    collected: List[CollectedFile],
    zip_path: Path,
    logger: logging.Logger,
    dry_run: bool = False,
) -> None:
    """Stream collected files into a ZIP archive."""
    if dry_run:
        logger.info("[DRY-RUN] Would create archive: %s", zip_path)
        for cf in collected:
            logger.info(
                "[DRY-RUN]   %s -> %s", cf.source_path, cf.archive_path
            )
        return

    logger.info("Creating archive: %s", zip_path)
    tmp_path = zip_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(
            str(tmp_path), "w",
            compression=ZIP_COMPRESSION,
            compresslevel=ZIP_COMPRESS_LEVEL,
        ) as zf:
            for cf in collected:
                logger.debug("  + %s -> %s", cf.source_path, cf.archive_path)
                info = _make_clean_zipinfo(cf.archive_path, cf.source_path)
                with zf.open(info, "w") as dest, \
                     cf.source_path.open("rb") as src:
                    shutil.copyfileobj(src, dest, length=STREAM_CHUNK_SIZE)

        tmp_path.replace(zip_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    archive_size_mb = zip_path.stat().st_size / (1024 * 1024)
    logger.info("Archive created (%.2f MiB): %s", archive_size_mb, zip_path)
