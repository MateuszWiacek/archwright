"""Delete old archives that exceed the retention limit."""

from __future__ import annotations

import logging
from pathlib import Path


def rotate_backups(
    target_dir: Path,
    backup_name: str,
    keep_last: int,
    logger: logging.Logger,
    dry_run: bool = False,
) -> None:
    """Delete the oldest backup pairs beyond *keep_last*."""
    if keep_last <= 0:
        logger.info("Rotation disabled (keep_last=%d)", keep_last)
        return

    pattern = f"{backup_name}_*.zip"
    # Filenames embed the timestamp, so lexical order is chronological order.
    existing_zips = sorted(target_dir.glob(pattern))

    logger.info(
        "Rotation: found %d archive(s) matching '%s', keeping last %d",
        len(existing_zips),
        pattern,
        keep_last,
    )

    if len(existing_zips) <= keep_last:
        logger.info("Rotation: nothing to delete")
        return

    to_delete = existing_zips[: len(existing_zips) - keep_last]

    for zip_file in to_delete:
        log_file = zip_file.with_suffix(".log")
        if dry_run:
            logger.info("[DRY-RUN] Would delete: %s", zip_file)
            if log_file.exists():
                logger.info("[DRY-RUN] Would delete: %s", log_file)
        else:
            logger.info("Deleting old backup: %s", zip_file)
            zip_file.unlink()
            if log_file.exists():
                logger.info("Deleting old log:    %s", log_file)
                log_file.unlink()
