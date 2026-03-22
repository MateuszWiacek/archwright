"""Filesystem scanning and file collection."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from backup.config import parse_glob
from backup.models import BackupConfig, CollectedFile

def _walk_safe(
    root: Path,
    visited_real_dirs: Optional[Set[Tuple[int, int]]] = None,
    logger: Optional[logging.Logger] = None,
) -> Iterator[Path]:
    """Yield files under *root* while skipping cycles and unreadable paths."""
    if visited_real_dirs is None:
        visited_real_dirs = set()

    try:
        real_root = root.resolve(strict=True)
    except OSError as exc:
        if logger:
            logger.warning("Cannot resolve directory, skipping: %s (%s)", root, exc)
        return

    try:
        dir_stat = real_root.stat()
    except OSError as exc:
        if logger:
            logger.warning("Cannot stat directory, skipping: %s (%s)", root, exc)
        return

    dir_id = (dir_stat.st_dev, dir_stat.st_ino)
    if dir_id in visited_real_dirs:
        if logger:
            logger.warning(
                "Symlink cycle detected, skipping: %s -> %s", root, real_root
            )
        return
    visited_real_dirs.add(dir_id)

    try:
        children = sorted(root.iterdir())
    except PermissionError as exc:
        if logger:
            logger.warning("Permission denied, skipping: %s (%s)", root, exc)
        return

    for child in children:
        try:
            real_child = child.resolve(strict=True)
        except OSError:
            if logger:
                logger.warning("Cannot resolve path, skipping: %s", child)
            continue

        if real_child.is_file():
            yield child
        elif real_child.is_dir():
            yield from _walk_safe(child, visited_real_dirs, logger)


def _matches_any(filename: str, patterns: List[str]) -> bool:
    """Return ``True`` if *filename* matches at least one glob *pattern*."""
    return any(fnmatch.fnmatch(filename, p) for p in patterns)


def collect_files(
    config: BackupConfig, logger: logging.Logger
) -> List[CollectedFile]:
    """Collect archive-bound files from configured source directories."""
    collected: List[CollectedFile] = []
    seen_archive_paths: Dict[str, Path] = {}

    for sf in config.subfolders:
        include_patterns = parse_glob(sf.include)
        exclude_patterns = parse_glob(sf.exclude) if sf.exclude else []
        source_root = sf.source_dir.resolve()
        subfolder_prefix = f"{sf.folder_name}/{sf.subfolder_name}"

        logger.info(
            "Scanning %s  (include=%s, exclude=%s)",
            source_root,
            sf.include,
            sf.exclude or "<none>",
        )

        matched_count = 0
        for file_path in _walk_safe(source_root, logger=logger):
            fname = file_path.name

            if not _matches_any(fname, include_patterns):
                continue
            if exclude_patterns and _matches_any(fname, exclude_patterns):
                logger.debug("Excluded by pattern: %s", file_path)
                continue

            try:
                real_path = file_path.resolve(strict=True)
            except OSError:
                logger.warning("Cannot resolve file, skipping: %s", file_path)
                continue

            rel_to_source = file_path.relative_to(source_root)
            archive_path = f"{subfolder_prefix}/{rel_to_source.as_posix()}"

            if archive_path in seen_archive_paths:
                existing = seen_archive_paths[archive_path]
                raise ValueError(
                    f"Destination collision in archive for '{archive_path}': "
                    f"'{real_path}' vs already collected '{existing}'"
                )
            seen_archive_paths[archive_path] = real_path

            collected.append(
                CollectedFile(source_path=real_path, archive_path=archive_path)
            )
            matched_count += 1

        logger.info(
            "  -> collected %d file(s) for %s", matched_count, subfolder_prefix
        )

    logger.info("Total files to archive: %d", len(collected))
    return collected
