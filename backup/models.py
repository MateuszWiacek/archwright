"""Shared data models for the backup package."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class SubfolderConfig:
    """Validated configuration for one file collection entry."""

    folder_name: str
    subfolder_name: str
    source_dir: Path
    include: str
    exclude: Optional[str] = None
    pre_command: Optional[str] = None
    post_command: Optional[str] = None


@dataclass
class DatabaseConfig:
    """Validated configuration for one database dump."""

    name: str
    provider: str
    archive_prefix: str

    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: Optional[str] = None
    dbname: Optional[str] = None
    pg_dump_path: str = "pg_dump"
    extra_args: List[str] = field(default_factory=list)

    container: Optional[str] = None
    docker_path: str = "docker"

    db_path: Optional[str] = None
    sqlite3_path: str = "sqlite3"

    # Optional service-control hooks.
    stop_command: Optional[str] = None
    start_command: Optional[str] = None


@dataclass
class BackupConfig:
    """Top-level validated backup configuration."""

    backup_name: str
    target_base_dir: Path
    keep_last: int
    subfolders: List[SubfolderConfig] = field(default_factory=list)
    databases: List[DatabaseConfig] = field(default_factory=list)
    log_level: str = "INFO"
    hook_timeout: int = 300
    dump_timeout: int = 3600


@dataclass
class CollectedFile:
    """A single file that will be placed into the archive."""

    source_path: Path
    archive_path: str
