"""JSON output formatters for CLI commands."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backup.models import BackupConfig, CollectedFile


def write_json(payload: Dict[str, Any]) -> None:
    """Print *payload* as indented JSON to stdout."""
    print(json.dumps(payload, indent=2))


def null_logger() -> logging.Logger:
    """Return a logger that drops every record.

    Used during JSON output so pipeline logging never pollutes stdout.
    """
    logger = logging.getLogger("backup.json")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


def retention_label(keep_last: int) -> str:
    """Human-readable retention description for ``keep_last``."""
    return "unlimited" if keep_last == 0 else f"{keep_last} most recent"


def archive_payload(archive: Path) -> Dict[str, Any]:
    """JSON payload describing a single archive file."""
    stat_result = archive.stat()
    log_file = archive.with_suffix(".log")
    return {
        "filename": archive.name,
        "path": str(archive),
        "size_bytes": stat_result.st_size,
        "size_mib": round(stat_result.st_size / (1024 * 1024), 2),
        "modified": datetime.fromtimestamp(
            stat_result.st_mtime
        ).isoformat(timespec="seconds"),
        "log": {
            "exists": log_file.exists(),
            "filename": log_file.name,
            "path": str(log_file),
        },
    }


def common_payload(config_path: Path, config: BackupConfig) -> Dict[str, Any]:
    """Shared header fields included in every command's JSON output."""
    return {
        "config": str(config_path),
        "backup_name": config.backup_name,
        "target_base_dir": str(config.target_base_dir),
        "keep_last": config.keep_last,
        "retention": retention_label(config.keep_last),
    }


def subfolders_payload(config: BackupConfig) -> List[Dict[str, Any]]:
    """JSON payload describing every configured subfolder."""
    return [
        {
            "folder": sf.folder_name,
            "subfolder": sf.subfolder_name,
            "source_dir": str(sf.source_dir),
            "include": sf.include,
            "exclude": sf.exclude,
            "has_hooks": bool(sf.pre_command or sf.post_command),
        }
        for sf in config.subfolders
    ]


def databases_payload(config: BackupConfig) -> List[Dict[str, Any]]:
    """JSON payload describing every configured database dump."""
    databases: List[Dict[str, Any]] = []
    for db in config.databases:
        item: Dict[str, Any] = {
            "name": db.name,
            "provider": db.provider,
            "archive_prefix": db.archive_prefix,
            "has_hooks": bool(db.stop_command or db.start_command),
        }
        if db.provider == "postgres":
            item.update({
                "host": db.host,
                "port": db.port,
                "user": db.user,
                "dbname": db.dbname,
            })
        elif db.provider == "docker_postgres":
            item.update({
                "container": db.container,
                "user": db.user,
                "dbname": db.dbname,
            })
        elif db.provider == "sqlite":
            item["db_path"] = db.db_path
        databases.append(item)
    return databases


def collected_file_payload(item: CollectedFile) -> Dict[str, Any]:
    """JSON payload describing a single collected file or dump entry."""
    return {
        "source_path": str(item.source_path),
        "archive_path": item.archive_path,
    }


def error_payload(
    *,
    config_path: Path,
    phase: str,
    error: str,
    config: Optional[BackupConfig] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a uniform error payload from any pipeline stage."""
    payload: Dict[str, Any]
    if config is None:
        payload = {"config": str(config_path)}
    else:
        payload = common_payload(config_path, config)
    payload.update({
        "ok": False,
        "phase": phase,
        "error": error,
    })
    if extra:
        payload.update(extra)
    return payload


def validate_payload(
    config_path: Path,
    config: BackupConfig,
    checks: List[Dict[str, Any]],
    *,
    ok: bool,
    phase: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """JSON payload for the ``validate`` command."""
    payload = common_payload(config_path, config)
    payload.update({
        "ok": ok,
        "checks": checks,
        "subfolders": subfolders_payload(config),
        "databases": databases_payload(config),
    })
    if phase is not None:
        payload["phase"] = phase
    if error is not None:
        payload["error"] = error
    return payload


def restore_entry_payload(entry: Any) -> Dict[str, Any]:
    """JSON payload for a single restore plan entry."""
    return {
        "archive_path": entry.archive_path,
        "target_path": str(entry.target_path),
        "target_exists": entry.target_path.exists(),
    }
