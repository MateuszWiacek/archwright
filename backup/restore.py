"""Restore files from a backup archive to their source directories."""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from backup.constants import STREAM_CHUNK_SIZE
from backup.models import BackupConfig


def _build_prefix_map(config: BackupConfig) -> Dict[str, Path]:
    prefix_map: Dict[str, Path] = {}
    for sf in config.subfolders:
        key = f"{sf.folder_name}/{sf.subfolder_name}"
        prefix_map[key] = sf.source_dir.resolve()
    return prefix_map


def _resolve_entry(
    archive_path: str, prefix_map: Dict[str, Path],
) -> Optional[Tuple[Path, str]]:
    parts = PurePosixPath(archive_path).parts
    if len(parts) < 3:
        return None
    prefix = f"{parts[0]}/{parts[1]}"
    target_dir = prefix_map.get(prefix)
    if target_dir is None:
        return None
    relative = str(PurePosixPath(*parts[2:]))
    return target_dir, relative


def _validate_archive(zip_path: Path) -> None:
    if not zip_path.is_file():
        raise ValueError(f"Archive does not exist: {zip_path}")
    if not zipfile.is_zipfile(str(zip_path)):
        raise ValueError(f"Not a valid ZIP archive: {zip_path}")


def _check_path_traversal(relative: str) -> None:
    for part in PurePosixPath(relative).parts:
        if part == "..":
            raise ValueError(
                f"Path traversal detected in archive entry: {relative}"
            )


class RestoreEntry:
    __slots__ = ("archive_path", "target_path")

    def __init__(self, archive_path: str, target_path: Path) -> None:
        self.archive_path = archive_path
        self.target_path = target_path

    def __repr__(self) -> str:
        return f"RestoreEntry({self.archive_path!r} -> {self.target_path})"


def plan_restore(
    zip_path: Path,
    config: BackupConfig,
    logger: logging.Logger,
    *,
    selected_prefixes: Optional[List[str]] = None,
) -> List[RestoreEntry]:
    _validate_archive(zip_path)
    prefix_map = _build_prefix_map(config)

    if selected_prefixes is not None:
        unknown = set(selected_prefixes) - set(prefix_map)
        if unknown:
            raise ValueError(
                f"Unknown prefix(es) not in config: {', '.join(sorted(unknown))}"
            )

    plan: List[RestoreEntry] = []
    skipped = 0

    with zipfile.ZipFile(str(zip_path), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            resolved = _resolve_entry(info.filename, prefix_map)
            if resolved is None:
                logger.debug("Skipping unmapped archive entry: %s", info.filename)
                skipped += 1
                continue
            target_dir, relative = resolved
            _check_path_traversal(relative)
            prefix = "/".join(PurePosixPath(info.filename).parts[:2])
            if selected_prefixes is not None and prefix not in selected_prefixes:
                continue
            plan.append(RestoreEntry(info.filename, target_dir / relative))

    logger.info("Restore plan: %d file(s) to extract, %d skipped", len(plan), skipped)
    return plan


def detect_conflicts(plan: List[RestoreEntry]) -> List[RestoreEntry]:
    return [e for e in plan if e.target_path.exists()]


def execute_restore(
    zip_path: Path,
    plan: List[RestoreEntry],
    logger: logging.Logger,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> int:
    if not plan:
        logger.info("Nothing to restore")
        return 0

    if not overwrite:
        conflicts = detect_conflicts(plan)
        if conflicts:
            for c in conflicts:
                logger.error("Would overwrite existing file: %s", c.target_path)
            raise ValueError(
                f"{len(conflicts)} file(s) already exist at target. "
                "Use --overwrite to replace them."
            )

    if dry_run:
        for entry in plan:
            logger.info(
                "[DRY-RUN] Would extract: %s -> %s",
                entry.archive_path, entry.target_path,
            )
        return len(plan)

    restored = 0
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        plan_index = {e.archive_path: e for e in plan}
        for info in zf.infolist():
            entry = plan_index.get(info.filename)
            if entry is None:
                continue
            target = entry.target_path
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            tmp_path = Path(tmp_name)
            try:
                with open(fd, "wb") as tmp_fh, zf.open(info, "r") as src:
                    shutil.copyfileobj(src, tmp_fh, length=STREAM_CHUNK_SIZE)
                tmp_path.replace(target)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            logger.debug("Restored: %s -> %s", info.filename, target)
            restored += 1

    logger.info("Restore complete: %d file(s) extracted", restored)
    return restored
