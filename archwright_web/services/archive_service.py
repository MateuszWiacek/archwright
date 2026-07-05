"""Archive listing and ZIP inspection."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional

from archwright_web.services.executor import (
    JsonCommandResult,
    LocalExecutor,
    local_executor,
)

MAX_INLINE_SIZE = 2 * 1024 * 1024  # 2 MiB; larger entries are download-only
STREAM_CHUNK_SIZE = 64 * 1024  # 64 KiB chunks for streaming


@dataclass
class ArchiveInfo:
    filename: str
    size_mb: float
    modified: datetime
    has_log: bool


@dataclass
class ArchiveListing:
    backup_name: str = ""
    target_base_dir: str = ""
    keep_last: int = 0
    archives: List[ArchiveInfo] = field(default_factory=list)


@dataclass
class ZipEntry:
    path: str
    size: int
    compressed_size: int
    is_dir: bool


class ArchiveListError(ValueError):
    """Raised when the CLI archive listing returns an error."""


def list_archives(target_dir: Path, backup_name: str) -> List[ArchiveInfo]:
    if not target_dir.is_dir():
        return []

    pattern = f"{backup_name}_*.zip"
    archives = sorted(target_dir.glob(pattern))
    result = []
    for archive in archives:
        stat = archive.stat()
        log_file = archive.with_suffix(".log")
        result.append(ArchiveInfo(
            filename=archive.name,
            size_mb=stat.st_size / (1024 * 1024),
            modified=datetime.fromtimestamp(stat.st_mtime),
            has_log=log_file.exists(),
        ))
    return result


def list_archives_from_config(
    config_path: Path,
    executor: LocalExecutor = local_executor,
) -> List[ArchiveInfo]:
    return read_archive_listing(config_path, executor).archives


def read_archive_listing(
    config_path: Path,
    executor: LocalExecutor = local_executor,
) -> ArchiveListing:
    result = executor.list_archives(config_path)
    if not result.ok:
        raise ArchiveListError(_executor_error(result))

    try:
        keep_last = int(result.payload.get("keep_last") or 0)
    except (TypeError, ValueError):
        keep_last = 0

    listing = ArchiveListing(
        backup_name=str(result.payload.get("backup_name") or ""),
        target_base_dir=str(result.payload.get("target_base_dir") or ""),
        keep_last=keep_last,
    )
    archives = []
    for item in result.payload.get("archives", []):
        try:
            archives.append(_archive_info_from_json(item))
        except (KeyError, TypeError, ValueError):
            continue
    listing.archives = archives
    return listing


def _executor_error(result: JsonCommandResult) -> str:
    error = result.payload.get("error") if result.payload else None
    if error:
        return str(error)
    if result.raw_output.strip():
        return result.raw_output.strip()
    return f"Archive listing failed with exit code {result.exit_code}"


def _archive_info_from_json(item: dict) -> ArchiveInfo:
    log_info = item.get("log", {})
    return ArchiveInfo(
        filename=str(item["filename"]),
        size_mb=float(item["size_mib"]),
        modified=datetime.fromisoformat(str(item["modified"])),
        has_log=bool(log_info.get("exists")),
    )


def list_zip_contents(zip_path: Path) -> List[ZipEntry]:
    if not zip_path.is_file():
        return []

    entries = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                entries.append(ZipEntry(
                    path=info.filename,
                    size=info.file_size,
                    compressed_size=info.compress_size,
                    is_dir=info.is_dir(),
                ))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid ZIP archive: {zip_path.name}") from exc
    return entries


def get_zip_entry_size(zip_path: Path, entry_path: str) -> Optional[int]:
    if not zip_path.is_file():
        return None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            try:
                info = zf.getinfo(entry_path)
                return info.file_size
            except KeyError:
                return None
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid ZIP archive: {zip_path.name}") from exc


def stream_zip_entry(
    zip_path: Path, entry_path: str
) -> Optional[Generator[bytes, None, None]]:
    if not zip_path.is_file():
        return None

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.getinfo(entry_path)
    except KeyError:
        return iter(())
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid ZIP archive: {zip_path.name}") from exc

    def _generate() -> Generator[bytes, None, None]:
        with zipfile.ZipFile(zip_path, "r") as zf:
            with zf.open(entry_path) as fp:
                while True:
                    chunk = fp.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

    return _generate()
