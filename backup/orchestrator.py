"""Backup pipeline orchestration: backup, restore, list, validate."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backup.archive import create_archive
from backup.collector import collect_files
from backup.config import load_config, validate_source_dirs
from backup.constants import EXIT_ERROR, EXIT_SUCCESS, TIMESTAMP_FORMAT
from backup.db_dump import run_dumps, validate_dump_prerequisites
from backup.json_output import (
    archive_payload,
    collected_file_payload,
    common_payload,
    error_payload,
    null_logger,
    restore_entry_payload,
    retention_label,
    validate_payload,
    write_json,
)
from backup.logging_setup import setup_logging
from backup.models import BackupConfig
from backup.rotation import rotate_backups


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _ensure_target_dir(
    target_dir: Path, logger: logging.Logger, dry_run: bool
) -> None:
    """Create *target_dir* if it does not exist."""
    if target_dir.exists():
        if not target_dir.is_dir():
            raise ValueError(f"target_base_dir is not a directory: {target_dir}")
        return

    if dry_run:
        logger.info("[DRY-RUN] Would create directory: %s", target_dir)
        return

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Created target directory: %s", target_dir)
    except OSError as exc:
        raise ValueError(
            f"Cannot create target_base_dir '{target_dir}': {exc}"
        ) from exc


def _cleanup_staging_dir(staging_dir: Optional[Path]) -> None:
    """Best-effort cleanup for temporary database dump staging."""
    if staging_dir and staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)


def _collect_service_hooks(
    config: BackupConfig,
) -> List[Tuple[str, str]]:
    """Return deduplicated ``(pre_command, post_command)`` pairs."""
    seen: set[str] = set()
    hooks: List[Tuple[str, str]] = []
    for sf in config.subfolders:
        if sf.pre_command and sf.pre_command not in seen:
            seen.add(sf.pre_command)
            # post_command is guaranteed non-None by config validation
            hooks.append((sf.pre_command, sf.post_command))  # type: ignore[arg-type]
    return hooks


def _run_hook(command: str, logger: logging.Logger, *, timeout: int = 300) -> None:
    """Execute a service-control hook."""
    logger.info("Running hook: %s", command)
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=timeout,
    )
    if result.stdout.strip():
        logger.debug("  stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.debug("  stderr: %s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(
            f"Hook failed (exit {result.returncode}): {command}\n"
            f"stderr: {result.stderr.strip()}"
        )


def _resolve_log_level(config_level: str, cli_verbose: bool, cli_quiet: bool) -> str:
    """Resolve the effective console log level."""
    if cli_verbose:
        return "DEBUG"
    if cli_quiet:
        return "WARNING"
    return config_level


def _validate_target_dir_preflight(target_dir: Path) -> None:
    """Validate target directory without creating it."""
    if target_dir.exists():
        if not target_dir.is_dir():
            raise ValueError(f"target_base_dir is not a directory: {target_dir}")
        return

    probe = target_dir
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent

    if not probe.exists():
        raise ValueError(
            f"Cannot validate target_base_dir '{target_dir}': "
            "no existing parent directory found"
        )
    if not probe.is_dir():
        raise ValueError(
            f"Cannot create target_base_dir '{target_dir}': "
            f"parent path is not a directory: {probe}"
        )
    if not os.access(probe, os.W_OK | os.X_OK):
        raise ValueError(
            f"Cannot create target_base_dir '{target_dir}': "
            f"parent directory is not writable: {probe}"
        )


# ---------------------------------------------------------------------------
# JSON dry-run workflows
# ---------------------------------------------------------------------------

def _backup_dry_run_json(config_path: Path) -> int:
    logger = null_logger()
    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)

    try:
        config = load_config(config_path)
    except ValueError as exc:
        write_json(error_payload(
            config_path=config_path,
            phase="config",
            error=str(exc),
        ))
        return EXIT_ERROR

    if config.target_base_dir.exists() and not config.target_base_dir.is_dir():
        write_json(error_payload(
            config_path=config_path,
            config=config,
            phase="target_base_dir",
            error=f"target_base_dir is not a directory: {config.target_base_dir}",
        ))
        return EXIT_ERROR

    try:
        validate_source_dirs(config)
    except ValueError as exc:
        write_json(error_payload(
            config_path=config_path,
            config=config,
            phase="source_dirs",
            error=str(exc),
        ))
        return EXIT_ERROR

    try:
        db_collected, _ = run_dumps(config, logger, dry_run=True)
    except Exception as exc:
        write_json(error_payload(
            config_path=config_path,
            config=config,
            phase="database_dumps",
            error=str(exc),
        ))
        return EXIT_ERROR

    try:
        file_collected = collect_files(config, logger)
    except ValueError as exc:
        write_json(error_payload(
            config_path=config_path,
            config=config,
            phase="file_collection",
            error=str(exc),
        ))
        return EXIT_ERROR

    entries = file_collected + db_collected
    service_hooks = [
        {"pre_command": pre_cmd, "post_command": post_cmd}
        for pre_cmd, post_cmd in _collect_service_hooks(config)
    ]
    zip_filename = f"{config.backup_name}_{timestamp}.zip"
    log_filename = f"{config.backup_name}_{timestamp}.log"

    payload = common_payload(config_path, config)
    payload.update({
        "ok": True,
        "dry_run": True,
        "timestamp": timestamp,
        "target": {
            "path": str(config.target_base_dir),
            "exists": config.target_base_dir.exists(),
            "would_create": not config.target_base_dir.exists(),
        },
        "archive": {
            "filename": zip_filename,
            "path": str(config.target_base_dir / zip_filename),
            "would_create": bool(entries),
        },
        "log": {
            "filename": log_filename,
            "path": str(config.target_base_dir / log_filename),
            "would_create": False,
        },
        "files": {
            "count": len(file_collected),
            "entries": [collected_file_payload(item) for item in file_collected],
        },
        "database_dumps": {
            "count": len(db_collected),
            "entries": [collected_file_payload(item) for item in db_collected],
        },
        "service_hooks": service_hooks,
        "total_entries": len(entries),
        "would_create_archive": bool(entries),
        "would_rotate": bool(entries) and config.keep_last > 0,
    })
    write_json(payload)
    return EXIT_SUCCESS


def _restore_dry_run_json(
    config_path: Path,
    archive_path: Path,
    *,
    selected_prefixes: Optional[List[str]] = None,
    overwrite: bool = False,
) -> int:
    from backup.restore import detect_conflicts, plan_restore

    logger = null_logger()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        write_json(error_payload(
            config_path=config_path,
            phase="config",
            error=str(exc),
        ))
        return EXIT_ERROR

    try:
        plan = plan_restore(
            archive_path,
            config,
            logger,
            selected_prefixes=selected_prefixes,
        )
    except ValueError as exc:
        write_json(error_payload(
            config_path=config_path,
            config=config,
            phase="restore_plan",
            error=str(exc),
            extra={
                "archive": str(archive_path),
                "selected_prefixes": selected_prefixes or [],
                "overwrite": overwrite,
                "dry_run": True,
            },
        ))
        return EXIT_ERROR

    conflicts = detect_conflicts(plan)
    if conflicts and not overwrite:
        write_json(error_payload(
            config_path=config_path,
            config=config,
            phase="conflicts",
            error=(
                f"{len(conflicts)} file(s) already exist at target. "
                "Use --overwrite to replace them."
            ),
            extra={
                "archive": str(archive_path),
                "dry_run": True,
                "overwrite": overwrite,
                "selected_prefixes": selected_prefixes or [],
                "plan_count": len(plan),
                "conflicts": [
                    restore_entry_payload(entry) for entry in conflicts
                ],
            },
        ))
        return EXIT_ERROR

    payload = common_payload(config_path, config)
    payload.update({
        "ok": True,
        "dry_run": True,
        "archive": str(archive_path),
        "overwrite": overwrite,
        "selected_prefixes": selected_prefixes or [],
        "plan_count": len(plan),
        "conflict_count": len(conflicts),
        "would_restore": len(plan),
        "plan": [restore_entry_payload(entry) for entry in plan],
        "conflicts": [restore_entry_payload(entry) for entry in conflicts],
    })
    write_json(payload)
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Pipeline entry points
# ---------------------------------------------------------------------------

def run(
    config_path: Path,
    dry_run: bool = False,
    *,
    verbose: bool = False,
    quiet: bool = False,
    json_output: bool = False,
) -> int:
    """Execute the full backup workflow and return an exit code."""
    if json_output:
        if not dry_run:
            write_json(error_payload(
                config_path=config_path,
                phase="arguments",
                error="backup --json is only supported with --dry-run",
            ))
            return EXIT_ERROR
        return _backup_dry_run_json(config_path)

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    no_archive_needed = False
    pipeline_failed = False
    post_hook_failed = False

    preliminary_logger = setup_logging()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        preliminary_logger.error("Configuration error: %s", exc)
        return EXIT_ERROR

    console_level = _resolve_log_level(config.log_level, verbose, quiet)

    zip_filename = f"{config.backup_name}_{timestamp}.zip"
    log_filename = f"{config.backup_name}_{timestamp}.log"
    zip_path = config.target_base_dir / zip_filename
    log_path = config.target_base_dir / log_filename

    try:
        _ensure_target_dir(config.target_base_dir, preliminary_logger, dry_run)
    except ValueError as exc:
        preliminary_logger.error("%s", exc)
        return EXIT_ERROR

    try:
        logger = setup_logging(
            log_file_path=log_path, dry_run=dry_run, console_level=console_level,
        )
    except OSError as exc:
        preliminary_logger.error("Cannot open log file '%s': %s", log_path, exc)
        return EXIT_ERROR
    logger.info("=" * 60)
    logger.info("Backup started  |  config=%s  dry_run=%s", config_path, dry_run)
    logger.info("Backup name:    %s", config.backup_name)
    logger.info("Target dir:     %s", config.target_base_dir)
    logger.info("Keep last:      %d", config.keep_last)
    logger.info("Timestamp:      %s", timestamp)
    logger.info("=" * 60)

    try:
        validate_source_dirs(config)
    except ValueError as exc:
        logger.error("Source validation failed: %s", exc)
        return EXIT_ERROR

    db_collected = []
    staging_dir = None
    if config.databases:
        try:
            db_collected, staging_dir = run_dumps(
                config, logger, dry_run=dry_run
            )
        except Exception as exc:
            logger.error("Database dump failed: %s", exc)
            return EXIT_ERROR

    service_hooks = _collect_service_hooks(config)
    post_commands_to_run: List[str] = []
    hook_timeout = config.hook_timeout

    try:
        for pre_cmd, post_cmd in service_hooks:
            try:
                if dry_run:
                    logger.info("[DRY-RUN] Would run pre_command: %s", pre_cmd)
                else:
                    _run_hook(pre_cmd, logger, timeout=hook_timeout)
            except Exception as hook_err:
                logger.error(
                    "pre_command failed: %s -- %s",
                    pre_cmd,
                    hook_err,
                )
                pipeline_failed = True
                break
            post_commands_to_run.append(post_cmd)

        if not pipeline_failed:
            try:
                collected = collect_files(config, logger)
            except ValueError as exc:
                logger.error("File collection failed: %s", exc)
                pipeline_failed = True
            else:
                collected.extend(db_collected)

                if not collected:
                    logger.warning("No files matched -- archive will not be created")
                    no_archive_needed = True
                else:
                    logger.info(
                        "Archiving %d file(s) into %s",
                        len(collected), zip_path.name,
                    )
                    try:
                        create_archive(collected, zip_path, logger, dry_run=dry_run)
                    except Exception as exc:
                        logger.error("Archive creation failed: %s", exc)
                        pipeline_failed = True

    finally:
        _cleanup_staging_dir(staging_dir)
        # Always try post hooks, even if collection or archiving failed.
        for post_cmd in reversed(post_commands_to_run):
            if dry_run:
                logger.info("[DRY-RUN] Would run post_command: %s", post_cmd)
            else:
                try:
                    _run_hook(post_cmd, logger, timeout=hook_timeout)
                except Exception as hook_err:
                    logger.error(
                        "post_command failed (service may be down!): %s -- %s",
                        post_cmd, hook_err,
                    )
                    post_hook_failed = True

    if post_hook_failed:
        return EXIT_ERROR
    if pipeline_failed:
        return EXIT_ERROR
    if no_archive_needed:
        return EXIT_SUCCESS

    try:
        rotate_backups(
            config.target_base_dir,
            config.backup_name,
            config.keep_last,
            logger,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.error("Rotation failed: %s", exc)
        return EXIT_ERROR

    logger.info("=" * 60)
    logger.info("Backup completed successfully")
    logger.info("=" * 60)
    return EXIT_SUCCESS


def run_restore(
    config_path: Path,
    archive_path: Path,
    *,
    selected_prefixes: Optional[List[str]] = None,
    overwrite: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    json_output: bool = False,
) -> int:
    """Execute the restore workflow and return an exit code."""
    from backup.restore import execute_restore, plan_restore

    if json_output:
        if not dry_run:
            write_json(error_payload(
                config_path=config_path,
                phase="arguments",
                error="restore --json is only supported with --dry-run",
                extra={"archive": str(archive_path)},
            ))
            return EXIT_ERROR
        return _restore_dry_run_json(
            config_path,
            archive_path,
            selected_prefixes=selected_prefixes,
            overwrite=overwrite,
        )

    logger = setup_logging()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_ERROR

    console_level = _resolve_log_level(config.log_level, verbose, quiet)
    logger = setup_logging(console_level=console_level)

    logger.info("=" * 60)
    logger.info("Restore started  |  archive=%s", archive_path)
    logger.info("Config:          %s", config_path)
    if selected_prefixes:
        logger.info("Prefixes:        %s", ", ".join(selected_prefixes))
    logger.info("Overwrite:       %s", overwrite)
    logger.info("Dry-run:         %s", dry_run)
    logger.info("=" * 60)

    try:
        plan = plan_restore(
            archive_path,
            config,
            logger,
            selected_prefixes=selected_prefixes,
        )
    except ValueError as exc:
        logger.error("Restore planning failed: %s", exc)
        return EXIT_ERROR

    if not plan:
        logger.warning("Nothing to restore -- plan is empty")
        return EXIT_SUCCESS

    try:
        count = execute_restore(
            archive_path,
            plan,
            logger,
            dry_run=dry_run,
            overwrite=overwrite,
        )
    except ValueError as exc:
        logger.error("Restore failed: %s", exc)
        return EXIT_ERROR

    logger.info("=" * 60)
    logger.info("Restore completed: %d file(s)", count)
    logger.info("=" * 60)
    return EXIT_SUCCESS


def run_list(
    config_path: Path,
    *,
    verbose: bool = False,
    quiet: bool = False,
    json_output: bool = False,
) -> int:
    """List available backup archives and return an exit code."""
    logger = null_logger() if json_output else setup_logging()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        if json_output:
            write_json({
                "ok": False,
                "config": str(config_path),
                "phase": "config",
                "error": str(exc),
            })
        else:
            logger.error("Configuration error: %s", exc)
        return EXIT_ERROR

    if not json_output:
        console_level = _resolve_log_level(config.log_level, verbose, quiet)
        logger = setup_logging(console_level=console_level)

    target = config.target_base_dir
    if not target.is_dir():
        if json_output:
            payload = common_payload(config_path, config)
            payload.update({
                "ok": False,
                "phase": "target_base_dir",
                "error": f"Target directory does not exist: {target}",
                "target_exists": False,
            })
            write_json(payload)
        else:
            logger.error("Target directory does not exist: %s", target)
        return EXIT_ERROR

    pattern = f"{config.backup_name}_*.zip"
    archives = sorted(target.glob(pattern))

    if json_output:
        payload = common_payload(config_path, config)
        archive_items: List[Dict[str, Any]] = []
        for archive in archives:
            try:
                archive_items.append(archive_payload(archive))
            except OSError:
                continue
        payload.update({
            "ok": True,
            "pattern": pattern,
            "archives": archive_items,
        })
        write_json(payload)
        return EXIT_SUCCESS

    if not archives:
        print(f"No archives found matching '{pattern}' in {target}")
        return EXIT_SUCCESS

    print(f"Archives in {target}  ({len(archives)} found):\n")
    for archive in archives:
        try:
            stat_result = archive.stat()
        except OSError:
            continue
        size_mb = stat_result.st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(stat_result.st_mtime)
        log_file = archive.with_suffix(".log")
        log_marker = "+" if log_file.exists() else "-"
        print(
            f"  {archive.name}  ({size_mb:>7.2f} MiB)  "
            f"{mtime:%Y-%m-%d %H:%M}  log:{log_marker}"
        )

    print(
        f"\nRetention policy: keep_last={config.keep_last} "
        f"({retention_label(config.keep_last)})"
    )
    return EXIT_SUCCESS


def run_validate(
    config_path: Path,
    *,
    verbose: bool = False,
    quiet: bool = False,
    json_output: bool = False,
) -> int:
    """Validate config and runtime prerequisites without running a backup."""
    logger = null_logger() if json_output else setup_logging()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        if json_output:
            write_json({
                "ok": False,
                "config": str(config_path),
                "phase": "config",
                "error": str(exc),
            })
        else:
            logger.error("Configuration error: %s", exc)
        return EXIT_ERROR

    if not json_output:
        console_level = _resolve_log_level(config.log_level, verbose, quiet)
        logger = setup_logging(console_level=console_level)

    checks: List[Dict[str, Any]] = [{"name": "config", "ok": True}]

    logger.info("Config loaded:     %s", config_path)
    logger.info("backup_name:       %s", config.backup_name)
    logger.info("target_base_dir:   %s", config.target_base_dir)
    logger.info("keep_last:         %d", config.keep_last)
    logger.info("subfolders:        %d", len(config.subfolders))
    logger.info("databases:         %d", len(config.databases))
    logger.info("log_level:         %s", config.log_level)
    logger.info("hook_timeout:      %d s", config.hook_timeout)
    logger.info("dump_timeout:      %d s", config.dump_timeout)

    try:
        _validate_target_dir_preflight(config.target_base_dir)
    except ValueError as exc:
        checks.append({"name": "target_base_dir", "ok": False})
        if json_output:
            write_json(validate_payload(
                config_path,
                config,
                checks,
                ok=False,
                phase="target_base_dir",
                error=str(exc),
            ))
        else:
            logger.error("Target directory validation failed: %s", exc)
        return EXIT_ERROR
    checks.append({"name": "target_base_dir", "ok": True})

    try:
        validate_source_dirs(config)
    except ValueError as exc:
        checks.append({"name": "source_dirs", "ok": False})
        if json_output:
            write_json(validate_payload(
                config_path,
                config,
                checks,
                ok=False,
                phase="source_dirs",
                error=str(exc),
            ))
        else:
            logger.error("Source validation failed: %s", exc)
        return EXIT_ERROR
    checks.append({"name": "source_dirs", "ok": True})

    try:
        validate_dump_prerequisites(config, logger)
    except ValueError as exc:
        checks.append({"name": "dump_prerequisites", "ok": False})
        if json_output:
            write_json(validate_payload(
                config_path,
                config,
                checks,
                ok=False,
                phase="dump_prerequisites",
                error=str(exc),
            ))
        else:
            logger.error("Database validation failed: %s", exc)
        return EXIT_ERROR
    checks.append({"name": "dump_prerequisites", "ok": True})

    if json_output:
        write_json(validate_payload(
            config_path,
            config,
            checks,
            ok=True,
        ))
        return EXIT_SUCCESS

    logger.info("All configured sources and runtime prerequisites are valid.")

    for sf in config.subfolders:
        logger.info(
            "  [OK] %s/%s -> %s (include=%s, exclude=%s)",
            sf.folder_name,
            sf.subfolder_name,
            sf.source_dir,
            sf.include,
            sf.exclude or "<none>",
        )

    for db in config.databases:
        logger.info(
            "  [OK] database '%s' (provider=%s)",
            db.name,
            db.provider,
        )

    logger.info("Validation passed.")
    return EXIT_SUCCESS
