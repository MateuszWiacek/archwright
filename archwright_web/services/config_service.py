"""Bridge between backup.config and the web UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from backup.config import load_config
from backup.models import BackupConfig


def load(config_path: Path) -> BackupConfig:
    return load_config(config_path)


def config_to_dict(config: BackupConfig) -> Dict[str, Any]:
    """Serialize a BackupConfig back to a YAML-compatible dict."""
    structure: Dict[str, Dict[str, Any]] = {}
    for sf in config.subfolders:
        folder = structure.setdefault(sf.folder_name, {})
        entry: Dict[str, Any] = {
            "source_dir": str(sf.source_dir),
            "include": sf.include,
        }
        if sf.exclude:
            entry["exclude"] = sf.exclude
        if sf.pre_command:
            entry["pre_command"] = sf.pre_command
        if sf.post_command:
            entry["post_command"] = sf.post_command
        folder[sf.subfolder_name] = entry

    result: Dict[str, Any] = {
        "backup_name": config.backup_name,
        "target_base_dir": str(config.target_base_dir),
        "keep_last": config.keep_last,
        "structure": structure,
    }

    if config.log_level != "INFO":
        result["log_level"] = config.log_level
    if config.hook_timeout != 300:
        result["hook_timeout"] = config.hook_timeout
    if config.dump_timeout != 3600:
        result["dump_timeout"] = config.dump_timeout

    if config.databases:
        databases: Dict[str, Any] = {}
        for db in config.databases:
            db_entry: Dict[str, Any] = {"provider": db.provider}

            if db.provider == "postgres":
                db_entry["dbname"] = db.dbname
                if db.host != "localhost":
                    db_entry["host"] = db.host
                if db.port != 5432:
                    db_entry["port"] = db.port
                if db.user != "postgres":
                    db_entry["user"] = db.user
                if db.password:
                    db_entry["password"] = db.password
                if db.pg_dump_path != "pg_dump":
                    db_entry["pg_dump_path"] = db.pg_dump_path
                if db.extra_args:
                    db_entry["extra_args"] = db.extra_args

            elif db.provider == "docker_postgres":
                db_entry["container"] = db.container
                db_entry["dbname"] = db.dbname
                if db.user != "postgres":
                    db_entry["user"] = db.user
                if db.docker_path != "docker":
                    db_entry["docker_path"] = db.docker_path
                if db.extra_args:
                    db_entry["extra_args"] = db.extra_args

            elif db.provider == "sqlite":
                db_entry["db_path"] = db.db_path
                if db.sqlite3_path != "sqlite3":
                    db_entry["sqlite3_path"] = db.sqlite3_path

            if db.stop_command:
                db_entry["stop_command"] = db.stop_command
            if db.start_command:
                db_entry["start_command"] = db.start_command

            databases[db.name] = db_entry
        result["databases"] = databases

    return result


def export_yaml(config: BackupConfig) -> str:
    return yaml.dump(
        config_to_dict(config),
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def dict_to_config(data: Dict[str, Any]) -> BackupConfig:
    """Build a BackupConfig from a form-submitted dict (same schema as YAML)."""
    import tempfile

    yaml_text = yaml.dump(data, default_flow_style=False, sort_keys=False)
    # NamedTemporaryFile with delete=False so we control the lifecycle; using
    # tempfile.mktemp() here would leave a TOCTOU race between picking the
    # name and creating the file (CWE-377).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8",
    ) as fh:
        fh.write(yaml_text)
        tmp = Path(fh.name)
    try:
        return load_config(tmp)
    finally:
        tmp.unlink(missing_ok=True)
