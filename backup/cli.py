"""CLI entry points for backup, restore, list, and validate."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from backup.archive import create_archive
from backup.collector import collect_files
from backup.config import load_config, validate_source_dirs
from backup.constants import EXIT_ERROR, EXIT_SUCCESS, TIMESTAMP_FORMAT
from backup.db_dump import run_dumps, validate_dump_prerequisites
from backup.logging_setup import setup_logging
from backup.models import BackupConfig
from backup.rotation import rotate_backups

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

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse and return command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="archwright",
        description="Config-driven backup, restore, and rotation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    def _add_common_flags(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument(
            "--verbose", "-v",
            action="store_true",
            default=False,
            help="Show DEBUG-level output on the console.",
        )
        group.add_argument(
            "--quiet", "-q",
            action="store_true",
            default=False,
            help="Suppress INFO messages; show only warnings and errors.",
        )

    backup_parser = sub.add_parser("backup", help="Create a backup archive.")
    backup_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the YAML configuration file.",
    )
    backup_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log planned actions without creating files or deleting anything.",
    )
    _add_common_flags(backup_parser)

    restore_parser = sub.add_parser(
        "restore",
        help="Restore files from a backup archive.",
    )
    restore_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the YAML configuration file (provides path mapping).",
    )
    restore_parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="Path to the .zip archive to restore from.",
    )
    restore_parser.add_argument(
        "--only",
        nargs="*",
        metavar="PREFIX",
        help="Restore only entries matching these folder/subfolder prefixes.",
    )
    restore_parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Replace existing files at target locations.",
    )
    restore_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log planned actions without extracting anything.",
    )
    _add_common_flags(restore_parser)

    list_parser = sub.add_parser("list", help="List available backup archives.")
    list_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the YAML configuration file.",
    )
    _add_common_flags(list_parser)

    validate_parser = sub.add_parser(
        "validate",
        help="Validate config and source directories without running a backup.",
    )
    validate_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the YAML configuration file.",
    )
    _add_common_flags(validate_parser)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(EXIT_ERROR)

    return args


def run(
    config_path: Path,
    dry_run: bool = False,
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    """Execute the full backup workflow and return an exit code."""
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
) -> int:
    """Execute the restore workflow and return an exit code."""
    from backup.restore import execute_restore, plan_restore

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
) -> int:
    """List available backup archives and return an exit code."""
    logger = setup_logging()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_ERROR

    console_level = _resolve_log_level(config.log_level, verbose, quiet)
    logger = setup_logging(console_level=console_level)

    target = config.target_base_dir
    if not target.is_dir():
        logger.error("Target directory does not exist: %s", target)
        return EXIT_ERROR

    pattern = f"{config.backup_name}_*.zip"
    archives = sorted(target.glob(pattern))

    if not archives:
        print(f"No archives found matching '{pattern}' in {target}")
        return EXIT_SUCCESS

    print(f"Archives in {target}  ({len(archives)} found):\n")
    for archive in archives:
        size_mb = archive.stat().st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(archive.stat().st_mtime)
        log_file = archive.with_suffix(".log")
        log_marker = "+" if log_file.exists() else "-"
        print(
            f"  {archive.name}  ({size_mb:>7.2f} MiB)  "
            f"{mtime:%Y-%m-%d %H:%M}  log:{log_marker}"
        )

    retention = "unlimited" if config.keep_last == 0 else f"{config.keep_last} most recent"
    print(f"\nRetention policy: keep_last={config.keep_last} ({retention})")
    return EXIT_SUCCESS


def run_validate(
    config_path: Path,
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    """Validate config and runtime prerequisites without running a backup."""
    logger = setup_logging()

    try:
        config = load_config(config_path)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return EXIT_ERROR

    console_level = _resolve_log_level(config.log_level, verbose, quiet)
    logger = setup_logging(console_level=console_level)

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
        logger.error("Target directory validation failed: %s", exc)
        return EXIT_ERROR

    try:
        validate_source_dirs(config)
    except ValueError as exc:
        logger.error("Source validation failed: %s", exc)
        return EXIT_ERROR

    try:
        validate_dump_prerequisites(config, logger)
    except ValueError as exc:
        logger.error("Database validation failed: %s", exc)
        return EXIT_ERROR

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


def main() -> None:
    args = parse_args()
    if args.command == "backup":
        sys.exit(run(
            config_path=args.config,
            dry_run=args.dry_run,
            verbose=args.verbose,
            quiet=args.quiet,
        ))
    if args.command == "restore":
        sys.exit(
            run_restore(
                config_path=args.config,
                archive_path=args.archive,
                selected_prefixes=args.only,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                verbose=args.verbose,
                quiet=args.quiet,
            )
        )
    if args.command == "list":
        sys.exit(run_list(
            config_path=args.config,
            verbose=args.verbose,
            quiet=args.quiet,
        ))
    if args.command == "validate":
        sys.exit(run_validate(
            config_path=args.config,
            verbose=args.verbose,
            quiet=args.quiet,
        ))
    sys.exit(EXIT_ERROR)
