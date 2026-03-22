"""End-to-end and CLI tests.

Covers:
  - Full successful run() producing .zip + .log with correct internal structure
  - --dry-run flag: no files created, output captured
  - CLI argument parsing validation
  - Archive internal structure matches YAML layout
  - Metadata stripping verified in real archive
  - Rotation after successful run
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict

import pytest
import yaml

from backup.cli import parse_args, run
from backup.constants import EXIT_ERROR, EXIT_SUCCESS, ZIP_EXTERNAL_ATTR


# ===================================================================
# Helpers
# ===================================================================
def _write_config(
    config_path: Path,
    src_json: Path,
    src_logs: Path,
    dest: Path,
    keep_last: int = 3,
) -> None:
    """Write a valid YAML config pointing at the given source directories."""
    data = {
        "backup_name": "e2e_test",
        "target_base_dir": str(dest),
        "keep_last": keep_last,
        "structure": {
            "data": {
                "json_files": {
                    "source_dir": str(src_json),
                    "include": "*.json",
                    "exclude": "*-tmp.json",
                },
            },
            "logs": {
                "app_logs": {
                    "source_dir": str(src_logs),
                    "include": "*.{log,txt}",
                },
            },
        },
    }
    config_path.write_text(yaml.dump(data), encoding="utf-8")


# ===================================================================
# Full run
# ===================================================================
class TestFullRun:
    """End-to-end: run() with a real filesystem and verify outputs."""

    @pytest.mark.e2e
    def test_successful_run_creates_zip_and_log(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """A valid config must produce exactly one .zip and one .log."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        exit_code = run(config_path, dry_run=False)

        assert exit_code == EXIT_SUCCESS

        zips = list(dest.glob("e2e_test_*.zip"))
        logs = list(dest.glob("e2e_test_*.log"))
        assert len(zips) == 1, f"Expected 1 zip, found {len(zips)}"
        assert len(logs) == 1, f"Expected 1 log, found {len(logs)}"

    @pytest.mark.e2e
    def test_archive_internal_structure(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """ZIP entries must mirror the YAML folder/subfolder layout exactly."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        run(config_path, dry_run=False)

        zip_path = next(dest.glob("e2e_test_*.zip"))
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            entries = sorted(zf.namelist())

        # Expected: data.json (included), cache-tmp.json (excluded)
        #           app.log + debug.txt (included), binary.bin (excluded)
        assert entries == sorted([
            "data/json_files/data.json",
            "logs/app_logs/app.log",
            "logs/app_logs/debug.txt",
        ])

    @pytest.mark.e2e
    def test_archive_file_content_integrity(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """Archived file content must match the source byte-for-byte."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        run(config_path, dry_run=False)

        zip_path = next(dest.glob("e2e_test_*.zip"))
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            assert zf.read("data/json_files/data.json") == b'{"ok": true}'
            assert zf.read("logs/app_logs/app.log") == b"2026-01-01 INFO start"

    @pytest.mark.e2e
    def test_archive_metadata_is_neutral(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """Every ZIP entry must carry the sanitised external_attr, not source perms."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        # Set unusual permissions on a source file to prove they're stripped
        (multi_source_tree["src_json"] / "data.json").chmod(0o777)

        run(config_path, dry_run=False)

        zip_path = next(dest.glob("e2e_test_*.zip"))
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            for info in zf.infolist():
                assert info.external_attr == ZIP_EXTERNAL_ATTR, (
                    f"{info.filename} has external_attr={oct(info.external_attr)}, "
                    f"expected {oct(ZIP_EXTERNAL_ATTR)}"
                )

    @pytest.mark.e2e
    def test_log_file_contains_summary(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """The companion .log must contain key operational lines."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        run(config_path, dry_run=False)

        log_path = next(dest.glob("e2e_test_*.log"))
        log_text = log_path.read_text(encoding="utf-8")

        assert "Backup started" in log_text
        assert "Backup completed successfully" in log_text
        assert "Archive created" in log_text
        assert "e2e_test" in log_text

    @pytest.mark.e2e
    def test_zip_and_log_share_timestamp(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """The .zip and .log filenames must contain the same timestamp."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        run(config_path, dry_run=False)

        zip_name = next(dest.glob("e2e_test_*.zip")).stem       # e2e_test_2026-...
        log_name = next(dest.glob("e2e_test_*.log")).stem
        # Both stems are identical (same timestamp)
        assert zip_name == log_name

    @pytest.mark.e2e
    def test_rotation_after_run(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """Run the pipeline 5 times with keep_last=3, verify only 3 survive."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
            keep_last=3,
        )

        # Pre-seed 4 old backups
        dest.mkdir(parents=True, exist_ok=True)
        for i in range(4):
            ts = f"2025-01-{i + 1:02d}_00-00-00"
            (dest / f"e2e_test_{ts}.zip").write_bytes(b"PK\x03\x04")
            (dest / f"e2e_test_{ts}.log").write_text("old log")

        # Run creates a 5th backup, rotation should prune to 3
        exit_code = run(config_path, dry_run=False)
        assert exit_code == EXIT_SUCCESS

        assert len(list(dest.glob("e2e_test_*.zip"))) == 3
        assert len(list(dest.glob("e2e_test_*.log"))) == 3

    @pytest.mark.e2e
    def test_post_command_failure_returns_error(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """A failed restart hook must make the whole run fail."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        config_path.write_text(
            yaml.dump(
                {
                    "backup_name": "e2e_test",
                    "target_base_dir": str(dest),
                    "keep_last": 0,
                    "structure": {
                        "data": {
                            "json_files": {
                                "source_dir": str(multi_source_tree["src_json"]),
                                "include": "*.json",
                                "pre_command": "true",
                                "post_command": "false",
                            },
                        },
                        "logs": {
                            "app_logs": {
                                "source_dir": str(multi_source_tree["src_logs"]),
                                "include": "*.{log,txt}",
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        exit_code = run(config_path, dry_run=False)

        assert exit_code == EXIT_ERROR
        log_path = next(dest.glob("e2e_test_*.log"))
        log_text = log_path.read_text(encoding="utf-8")
        assert "post_command failed" in log_text
        assert "Backup completed successfully" not in log_text


# ===================================================================
# Dry run
# ===================================================================
class TestDryRun:
    """Verify --dry-run produces no side effects."""

    @pytest.mark.e2e
    def test_dry_run_creates_no_files(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path, capsys
    ) -> None:
        """In dry-run mode, target_base_dir must remain empty (or non-existent)."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        exit_code = run(config_path, dry_run=True)

        assert exit_code == EXIT_SUCCESS
        # dest should not exist or be empty -- dry-run doesn't create it
        if dest.exists():
            assert list(dest.iterdir()) == []

    @pytest.mark.e2e
    def test_dry_run_output_contains_markers(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path, capsys
    ) -> None:
        """Stdout must contain [DRY-RUN] markers for planned actions."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
        )

        run(config_path, dry_run=True)

        captured = capsys.readouterr().out
        assert "[DRY-RUN]" in captured
        assert "Would create archive" in captured

    @pytest.mark.e2e
    def test_dry_run_does_not_delete_old_backups(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """Pre-existing backups must survive a dry-run even when over the limit."""
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        _write_config(
            config_path,
            multi_source_tree["src_json"],
            multi_source_tree["src_logs"],
            dest,
            keep_last=1,
        )

        # Pre-seed 5 old backups
        dest.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            ts = f"2025-06-{i + 1:02d}_00-00-00"
            (dest / f"e2e_test_{ts}.zip").write_bytes(b"PK")
            (dest / f"e2e_test_{ts}.log").write_text("log")

        run(config_path, dry_run=True)

        # All 5 must still exist
        assert len(list(dest.glob("e2e_test_*.zip"))) == 5


# ===================================================================
# CLI argument parsing
# ===================================================================
class TestCLIArgParsing:
    """Verify argparse configuration."""

    @pytest.mark.e2e
    def test_no_subcommand_exits(self) -> None:
        """No subcommand must cause SystemExit."""
        with pytest.raises(SystemExit):
            parse_args([])

    @pytest.mark.e2e
    def test_backup_config_required(self) -> None:
        """backup without --config must cause SystemExit."""
        with pytest.raises(SystemExit):
            parse_args(["backup"])

    @pytest.mark.e2e
    def test_backup_config_path_parsed(self, tmp_path: Path) -> None:
        args = parse_args(["backup", "--config", str(tmp_path / "c.yaml")])
        assert args.command == "backup"
        assert args.config == tmp_path / "c.yaml"
        assert args.dry_run is False

    @pytest.mark.e2e
    def test_backup_dry_run_flag(self, tmp_path: Path) -> None:
        args = parse_args(
            ["backup", "--config", str(tmp_path / "c.yaml"), "--dry-run"]
        )
        assert args.command == "backup"
        assert args.dry_run is True

    @pytest.mark.e2e
    def test_backup_unknown_flag_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["backup", "--config", "c.yaml", "--unknown-flag"])

    @pytest.mark.e2e
    def test_restore_requires_archive(self, tmp_path: Path) -> None:
        """restore without --archive must cause SystemExit."""
        with pytest.raises(SystemExit):
            parse_args(["restore", "--config", str(tmp_path / "c.yaml")])

    @pytest.mark.e2e
    def test_restore_all_flags(self, tmp_path: Path) -> None:
        args = parse_args(
            [
                "restore",
                "--config",
                str(tmp_path / "c.yaml"),
                "--archive",
                str(tmp_path / "backup.zip"),
                "--only",
                "app/config",
                "logs/app",
                "--overwrite",
                "--dry-run",
            ]
        )
        assert args.command == "restore"
        assert args.config == tmp_path / "c.yaml"
        assert args.archive == tmp_path / "backup.zip"
        assert args.only == ["app/config", "logs/app"]
        assert args.overwrite is True
        assert args.dry_run is True

    @pytest.mark.e2e
    def test_list_subcommand(self, tmp_path: Path) -> None:
        args = parse_args(["list", "--config", str(tmp_path / "c.yaml")])
        assert args.command == "list"
        assert args.config == tmp_path / "c.yaml"
