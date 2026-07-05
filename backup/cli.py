"""Command-line interface: argparse and dispatch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from backup.constants import EXIT_ERROR, EXIT_SUCCESS
from backup.orchestrator import run, run_list, run_restore, run_validate


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
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


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=False,
        help="Write machine-readable JSON to stdout.",
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse and return command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="archwright",
        description="Config-driven backup, restore, and rotation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # backup
    backup_parser = sub.add_parser("backup", help="Create a backup archive.")
    backup_parser.add_argument(
        "--config", type=Path, required=True,
        help="Path to the YAML configuration file.",
    )
    backup_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Log planned actions without creating files or deleting anything.",
    )
    _add_json_flag(backup_parser)
    _add_common_flags(backup_parser)

    # restore
    restore_parser = sub.add_parser(
        "restore", help="Restore files from a backup archive.",
    )
    restore_parser.add_argument(
        "--config", type=Path, required=True,
        help="Path to the YAML configuration file (provides path mapping).",
    )
    restore_parser.add_argument(
        "--archive", type=Path, required=True,
        help="Path to the .zip archive to restore from.",
    )
    restore_parser.add_argument(
        "--only", nargs="*", metavar="PREFIX",
        help="Restore only entries matching these folder/subfolder prefixes.",
    )
    restore_parser.add_argument(
        "--overwrite", action="store_true", default=False,
        help="Replace existing files at target locations.",
    )
    restore_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Log planned actions without extracting anything.",
    )
    _add_json_flag(restore_parser)
    _add_common_flags(restore_parser)

    # list
    list_parser = sub.add_parser("list", help="List available backup archives.")
    list_parser.add_argument(
        "--config", type=Path, required=True,
        help="Path to the YAML configuration file.",
    )
    _add_json_flag(list_parser)
    _add_common_flags(list_parser)

    # validate
    validate_parser = sub.add_parser(
        "validate",
        help="Validate config and source directories without running a backup.",
    )
    validate_parser.add_argument(
        "--config", type=Path, required=True,
        help="Path to the YAML configuration file.",
    )
    _add_json_flag(validate_parser)
    _add_common_flags(validate_parser)

    # serve
    serve_parser = sub.add_parser("serve", help="Start the web UI.")
    serve_config = serve_parser.add_mutually_exclusive_group(required=True)
    serve_config.add_argument(
        "--config", type=Path,
        help="Path to the YAML configuration file.",
    )
    serve_config.add_argument(
        "--config-dir", type=Path,
        help="Directory containing one or more YAML configuration files.",
    )
    serve_config.add_argument(
        "--inventory", type=Path,
        help="Inventory YAML for planned multi-node control.",
    )
    serve_parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port", type=int, default=8471,
        help="Port to listen on (default: 8471).",
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(EXIT_ERROR)

    return args


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "backup":
        return run(
            config_path=args.config,
            dry_run=args.dry_run,
            verbose=args.verbose,
            quiet=args.quiet,
            json_output=args.json_output,
        )
    if args.command == "restore":
        return run_restore(
            config_path=args.config,
            archive_path=args.archive,
            selected_prefixes=args.only,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            verbose=args.verbose,
            quiet=args.quiet,
            json_output=args.json_output,
        )
    if args.command == "list":
        return run_list(
            config_path=args.config,
            verbose=args.verbose,
            quiet=args.quiet,
            json_output=args.json_output,
        )
    if args.command == "validate":
        return run_validate(
            config_path=args.config,
            verbose=args.verbose,
            quiet=args.quiet,
            json_output=args.json_output,
        )
    if args.command == "serve":
        from archwright_web.server import run_server

        run_server(
            config_path=args.config,
            config_dir=args.config_dir,
            inventory_path=args.inventory,
            host=args.host,
            port=args.port,
        )
        return EXIT_SUCCESS
    return EXIT_ERROR


def main() -> None:
    sys.exit(_dispatch(parse_args()))
