"""YAML configuration loading and validation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from backup.constants import (
    DEFAULT_DUMP_TIMEOUT,
    DEFAULT_HOOK_TIMEOUT,
    DEFAULT_LOG_LEVEL,
)
from backup.models import BackupConfig, DatabaseConfig, SubfolderConfig

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_GLOB_META = set("*?[]")

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "ERROR: PyYAML is required. Install it with:  pip install pyyaml\n"
    )
    sys.exit(1)

def _require_field(data: dict, key: str, context: str) -> object:
    """Return ``data[key]`` or raise with a clear message."""
    if key not in data:
        raise ValueError(f"Missing required field '{key}' in {context}")
    return data[key]


def _require_string(
    data: dict,
    key: str,
    context: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Return a string field or raise a validation error."""
    value = _require_field(data, key, context)
    if not isinstance(value, str):
        raise ValueError(f"Field '{key}' in {context} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"Field '{key}' in {context} must not be empty")
    return value


def _optional_string(
    data: dict,
    key: str,
    context: str,
    *,
    allow_empty: bool = False,
) -> Optional[str]:
    """Return an optional string field or raise a validation error."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Field '{key}' in {context} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"Field '{key}' in {context} must not be empty")
    return value


def _require_int(data: dict, key: str, context: str) -> int:
    """Return an integer field, rejecting booleans and other coercions."""
    value = _require_field(data, key, context)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Field '{key}' in {context} must be an integer")
    return value


def _validate_backup_name(value: str) -> str:
    """Validate ``backup_name`` as a literal filename prefix for rotation globs."""
    backup_name = value.strip()
    if not backup_name:
        raise ValueError("'backup_name' must not be empty")
    if "/" in backup_name or "\\" in backup_name:
        raise ValueError("'backup_name' must not contain path separators")
    if backup_name in {".", ".."}:
        raise ValueError("'backup_name' must not be '.' or '..'")
    bad = _GLOB_META.intersection(backup_name)
    if bad:
        raise ValueError(
            f"'backup_name' must not contain glob metacharacters: "
            f"{''.join(sorted(bad))}"
        )
    return backup_name


def _validate_segment_name(value: str, kind: str, context: str) -> str:
    """Validate one safe archive path segment from YAML."""
    name = value.strip()
    if not name:
        raise ValueError(f"{kind} name in {context} must not be empty")
    if "/" in name or "\\" in name:
        raise ValueError(
            f"{kind} name '{name}' in {context} must not contain path separators"
        )
    if name in {".", ".."}:
        raise ValueError(
            f"{kind} name '{name}' in {context} must not be '.' or '..'"
        )
    return name


_SUPPORTED_DB_PROVIDERS = {"postgres", "docker_postgres", "sqlite"}


def _parse_databases(raw: dict) -> List[DatabaseConfig]:
    """Parse optional database dump definitions."""
    result: List[DatabaseConfig] = []

    for db_name, db_data in raw.items():
        ctx = f"database '{db_name}'"
        if not isinstance(db_name, str) or not db_name.strip():
            raise ValueError("Database names must be non-empty strings")
        db_name = _validate_segment_name(db_name, "Database", "'databases'")
        if not isinstance(db_data, dict):
            raise ValueError(f"{ctx} must be a mapping")

        provider = _require_string(db_data, "provider", ctx).strip().lower()
        if provider not in _SUPPORTED_DB_PROVIDERS:
            raise ValueError(
                f"Unsupported provider '{provider}' in {ctx}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_DB_PROVIDERS))}"
            )

        extra_args_raw = db_data.get("extra_args", [])
        if not isinstance(extra_args_raw, list):
            raise ValueError(f"'extra_args' in {ctx} must be a list")
        extra_args = [str(a) for a in extra_args_raw]

        stop_command = _optional_string(db_data, "stop_command", ctx)
        start_command = _optional_string(db_data, "start_command", ctx)
        if stop_command and not start_command:
            raise ValueError(
                f"{ctx}: 'stop_command' requires a matching 'start_command'"
            )
        if start_command and not stop_command:
            raise ValueError(
                f"{ctx}: 'start_command' without 'stop_command' makes no sense"
            )

        host = "localhost"
        port = 5432
        user = "postgres"
        password: Optional[str] = None
        dbname: Optional[str] = None
        pg_dump_path = "pg_dump"
        container: Optional[str] = None
        docker_path = "docker"
        db_path: Optional[str] = None
        sqlite3_path = "sqlite3"

        if provider == "postgres":
            dbname = _require_string(db_data, "dbname", ctx)
            host = _optional_string(db_data, "host", ctx) or "localhost"
            user = _optional_string(db_data, "user", ctx) or "postgres"
            password = _optional_string(db_data, "password", ctx)
            pg_dump_path = _optional_string(db_data, "pg_dump_path", ctx) or "pg_dump"
            raw_port = db_data.get("port", 5432)
            if not isinstance(raw_port, int) or isinstance(raw_port, bool):
                raise ValueError(f"'port' in {ctx} must be an integer")
            port = raw_port

        elif provider == "docker_postgres":
            container = _require_string(db_data, "container", ctx)
            dbname = _require_string(db_data, "dbname", ctx)
            user = _optional_string(db_data, "user", ctx) or "postgres"
            docker_path = _optional_string(db_data, "docker_path", ctx) or "docker"

        elif provider == "sqlite":
            db_path = _require_string(db_data, "db_path", ctx)
            sqlite3_path = (
                _optional_string(db_data, "sqlite3_path", ctx) or "sqlite3"
            )

        archive_prefix = f"databases/{db_name}"

        result.append(
            DatabaseConfig(
                name=db_name,
                provider=provider,
                archive_prefix=archive_prefix,
                host=host,
                port=port,
                user=user,
                password=password,
                dbname=dbname,
                pg_dump_path=pg_dump_path,
                extra_args=extra_args,
                container=container,
                docker_path=docker_path,
                db_path=db_path,
                sqlite3_path=sqlite3_path,
                stop_command=stop_command,
                start_command=start_command,
            )
        )

    return result


def parse_glob(pattern: str) -> List[str]:
    """Expand brace shorthand into standard glob patterns."""
    pattern = pattern.strip()
    if "{" in pattern and "}" in pattern:
        prefix, rest = pattern.split("{", 1)
        alternatives, suffix = rest.split("}", 1)
        return [f"{prefix}{alt.strip()}{suffix}" for alt in alternatives.split(",")]
    return [pattern]


def load_config(config_path: Path) -> BackupConfig:
    """Read, parse, and validate a YAML backup configuration file."""
    if not config_path.is_file():
        raise ValueError(f"Config file does not exist: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"Malformed YAML in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("YAML root must be a mapping")

    backup_name = _validate_backup_name(
        _require_string(data, "backup_name", "root")
    )
    target_base_dir = Path(_require_string(data, "target_base_dir", "root"))
    keep_last = _require_int(data, "keep_last", "root")

    if keep_last < 0:
        raise ValueError(f"'keep_last' must be >= 0, got {keep_last}")

    structure = _require_field(data, "structure", "root")
    if not isinstance(structure, dict):
        raise ValueError("'structure' must be a mapping of folders")

    subfolders: List[SubfolderConfig] = []

    for folder_name, folder_content in structure.items():
        if not isinstance(folder_name, str):
            raise ValueError("Folder names in 'structure' must be strings")
        folder_name = _validate_segment_name(
            folder_name, "Folder", "'structure'"
        )
        if not isinstance(folder_content, dict):
            raise ValueError(
                f"Folder '{folder_name}' must contain subfolder mappings"
            )
        for subfolder_name, sf_content in folder_content.items():
            if not isinstance(subfolder_name, str):
                raise ValueError(
                    f"Subfolder names in folder '{folder_name}' must be strings"
                )
            subfolder_name = _validate_segment_name(
                subfolder_name, "Subfolder", f"folder '{folder_name}'"
            )
            if not isinstance(sf_content, dict):
                raise ValueError(
                    f"Subfolder '{folder_name}/{subfolder_name}' must be a mapping"
                )

            source_dir = Path(
                _require_string(
                    sf_content,
                    "source_dir",
                    f"'{folder_name}/{subfolder_name}'",
                )
            )
            include_raw = _require_string(
                sf_content,
                "include",
                f"'{folder_name}/{subfolder_name}'",
            )
            exclude_raw = _optional_string(
                sf_content,
                "exclude",
                f"'{folder_name}/{subfolder_name}'",
            )

            # Service control around file collection -- OPT-IN ONLY
            sf_ctx = f"'{folder_name}/{subfolder_name}'"
            pre_command = _optional_string(sf_content, "pre_command", sf_ctx)
            post_command = _optional_string(sf_content, "post_command", sf_ctx)
            if pre_command and not post_command:
                raise ValueError(
                    f"{sf_ctx}: 'pre_command' requires a matching 'post_command'"
                )
            if post_command and not pre_command:
                raise ValueError(
                    f"{sf_ctx}: 'post_command' without 'pre_command' makes no sense"
                )

            subfolders.append(
                SubfolderConfig(
                    folder_name=folder_name,
                    subfolder_name=subfolder_name,
                    source_dir=source_dir,
                    include=include_raw,
                    exclude=exclude_raw,
                    pre_command=pre_command,
                    post_command=post_command,
                )
            )

    if not subfolders:
        raise ValueError("'structure' must define at least one subfolder")

    databases: List[DatabaseConfig] = []
    raw_dbs = data.get("databases")
    if raw_dbs is not None:
        if not isinstance(raw_dbs, dict):
            raise ValueError("'databases' must be a mapping")
        databases = _parse_databases(raw_dbs)

    log_level = DEFAULT_LOG_LEVEL
    raw_log_level = data.get("log_level")
    if raw_log_level is not None:
        if not isinstance(raw_log_level, str):
            raise ValueError("'log_level' must be a string")
        log_level = raw_log_level.strip().upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"'log_level' must be one of {', '.join(sorted(_VALID_LOG_LEVELS))}, "
                f"got '{raw_log_level}'"
            )

    hook_timeout = DEFAULT_HOOK_TIMEOUT
    raw_hook_timeout = data.get("hook_timeout")
    if raw_hook_timeout is not None:
        if not isinstance(raw_hook_timeout, int) or isinstance(raw_hook_timeout, bool):
            raise ValueError("'hook_timeout' must be an integer")
        if raw_hook_timeout <= 0:
            raise ValueError("'hook_timeout' must be > 0")
        hook_timeout = raw_hook_timeout

    dump_timeout = DEFAULT_DUMP_TIMEOUT
    raw_dump_timeout = data.get("dump_timeout")
    if raw_dump_timeout is not None:
        if not isinstance(raw_dump_timeout, int) or isinstance(raw_dump_timeout, bool):
            raise ValueError("'dump_timeout' must be an integer")
        if raw_dump_timeout <= 0:
            raise ValueError("'dump_timeout' must be > 0")
        dump_timeout = raw_dump_timeout

    return BackupConfig(
        backup_name=backup_name,
        target_base_dir=target_base_dir,
        keep_last=keep_last,
        subfolders=subfolders,
        databases=databases,
        log_level=log_level,
        hook_timeout=hook_timeout,
        dump_timeout=dump_timeout,
    )


def validate_source_dirs(config: BackupConfig) -> None:
    """Ensure every configured ``source_dir`` exists and is a directory."""
    for sf in config.subfolders:
        resolved = sf.source_dir.resolve()
        if not resolved.exists():
            raise ValueError(f"source_dir does not exist: {sf.source_dir}")
        if not resolved.is_dir():
            raise ValueError(f"source_dir is not a directory: {sf.source_dir}")
