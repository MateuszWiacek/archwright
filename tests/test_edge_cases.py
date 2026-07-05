"""Edge cases and resilience tests.

Covers:
  - Missing source_dir → hard error
  - source_dir is a file, not a directory → hard error
  - Target base directory auto-creation (including nested)
  - Permission-denied on source directory (monkeypatched)
  - Atomic write: no partial .zip.tmp left on failure
  - run() returns EXIT_ERROR on invalid config
  - run() returns EXIT_ERROR on missing source
  - Dangling symlink is skipped gracefully
  - Empty result set produces no archive
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest
import yaml

from backup.orchestrator import run, _ensure_target_dir
from backup.collector import collect_files, _walk_safe
from backup.config import validate_source_dirs
from backup.archive import create_archive
from backup.constants import EXIT_ERROR, EXIT_SUCCESS
from backup.models import BackupConfig, CollectedFile, SubfolderConfig


# ===================================================================
# Missing / invalid source_dir
# ===================================================================
class TestMissingSourceDir:
    """source_dir must exist and be a directory -- hard stop otherwise."""

    @pytest.mark.edge
    def test_nonexistent_source_dir(self, tmp_path: Path) -> None:
        config = BackupConfig(
            backup_name="test",
            target_base_dir=tmp_path,
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="f",
                    subfolder_name="sf",
                    source_dir=tmp_path / "does_not_exist",
                    include="*",
                ),
            ],
        )
        with pytest.raises(ValueError, match="does not exist"):
            validate_source_dirs(config)

    @pytest.mark.edge
    def test_source_dir_is_a_file(self, tmp_path: Path) -> None:
        """A regular file passed as source_dir must fail validation."""
        fake_dir = tmp_path / "actually_a_file.txt"
        fake_dir.write_text("I am not a directory")

        config = BackupConfig(
            backup_name="test",
            target_base_dir=tmp_path,
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="f",
                    subfolder_name="sf",
                    source_dir=fake_dir,
                    include="*",
                ),
            ],
        )
        with pytest.raises(ValueError, match="not a directory"):
            validate_source_dirs(config)

    @pytest.mark.edge
    def test_run_returns_error_on_missing_source(
        self, tmp_path: Path
    ) -> None:
        """Full pipeline must exit with code 1 when a source_dir is missing."""
        config_path = tmp_path / "config.yaml"
        dest = tmp_path / "dest"
        data = {
            "backup_name": "test",
            "target_base_dir": str(dest),
            "keep_last": 0,
            "structure": {
                "f": {
                    "sf": {
                        "source_dir": str(tmp_path / "ghost"),
                        "include": "*",
                    },
                },
            },
        }
        config_path.write_text(yaml.dump(data), encoding="utf-8")

        exit_code = run(config_path, dry_run=False)
        assert exit_code == EXIT_ERROR

        # No partial outputs should exist
        if dest.exists():
            assert list(dest.glob("*.zip")) == []


# ===================================================================
# Target base directory creation
# ===================================================================
class TestTargetDirCreation:
    """target_base_dir must be created automatically if it doesn't exist."""

    @pytest.mark.edge
    def test_nested_target_dir_created(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """Deeply nested target_base_dir should be created by the pipeline."""
        config_path = tmp_path / "config.yaml"
        dest = tmp_path / "level1" / "level2" / "level3" / "backups"
        data = {
            "backup_name": "test",
            "target_base_dir": str(dest),
            "keep_last": 0,
            "structure": {
                "data": {
                    "files": {
                        "source_dir": str(multi_source_tree["src_json"]),
                        "include": "*",
                    },
                },
            },
        }
        config_path.write_text(yaml.dump(data), encoding="utf-8")

        exit_code = run(config_path, dry_run=False)

        assert exit_code == EXIT_SUCCESS
        assert dest.is_dir()
        assert len(list(dest.glob("test_*.zip"))) == 1

    @pytest.mark.edge
    def test_ensure_target_dir_raises_on_failure(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """If mkdir fails (e.g. read-only parent), _ensure_target_dir must raise."""
        # Use monkeypatch to make mkdir always fail
        bad_path = tmp_path / "impossible" / "target"
        with patch.object(Path, "mkdir", side_effect=OSError("Permission denied")):
            with pytest.raises(ValueError, match="Cannot create"):
                _ensure_target_dir(bad_path, logger, dry_run=False)

    @pytest.mark.edge
    def test_existing_target_dir_is_fine(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """An already-existing target_base_dir must not cause any error."""
        dest = tmp_path / "existing"
        dest.mkdir()

        # Should return without error
        _ensure_target_dir(dest, logger, dry_run=False)
        assert dest.is_dir()

    @pytest.mark.edge
    def test_dry_run_rejects_target_path_that_is_file(
        self, tmp_path: Path
    ) -> None:
        """Dry-run must validate that target_base_dir is a directory path."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("content")

        dest_file = tmp_path / "not_a_directory"
        dest_file.write_text("blocking file")

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "backup_name": "test",
                    "target_base_dir": str(dest_file),
                    "keep_last": 1,
                    "structure": {
                        "f": {
                            "sf": {
                                "source_dir": str(src),
                                "include": "*",
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        assert run(config_path, dry_run=True) == EXIT_ERROR


# ===================================================================
# Permission denied on source files / directories
# ===================================================================
class TestPermissionDenied:
    """Verify graceful handling of unreadable directories/files."""

    @pytest.mark.edge
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Permission model differs on Windows",
    )
    @pytest.mark.skipif(
        os.getuid() == 0,
        reason="Root can read anything, skip permission tests",
    )
    def test_unreadable_subdirectory_skipped(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """A subdirectory with 0o000 perms should be logged and skipped, not crash."""
        root = tmp_path / "mixed"
        readable = root / "ok"
        unreadable = root / "forbidden"

        readable.mkdir(parents=True)
        unreadable.mkdir(parents=True)
        (readable / "visible.txt").write_text("I can see")
        (unreadable / "hidden.txt").write_text("You shall not pass")

        # Remove all permissions from the directory
        unreadable.chmod(0o000)

        try:
            files = list(_walk_safe(root, logger=logger))
            names = [f.name for f in files]

            # visible.txt found, hidden.txt skipped
            assert "visible.txt" in names
            assert "hidden.txt" not in names
        finally:
            # Restore permissions so tmp_path cleanup doesn't fail
            unreadable.chmod(0o755)

    @pytest.mark.edge
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Permission model differs on Windows",
    )
    @pytest.mark.skipif(
        os.getuid() == 0,
        reason="Root can read anything, skip permission tests",
    )
    def test_unreadable_file_does_not_crash_archive(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """If a collected file becomes unreadable before archiving, it should raise."""
        src = tmp_path / "readable.txt"
        src.write_text("temporary content")

        collected = [
            CollectedFile(source_path=src.resolve(), archive_path="f/sf/readable.txt")
        ]

        # Remove read permission after collection
        src.chmod(0o000)

        zip_path = tmp_path / "test.zip"
        try:
            with pytest.raises(PermissionError):
                create_archive(collected, zip_path, logger)

            # Atomic write: no partial .zip or .zip.tmp should remain
            assert not zip_path.exists()
            assert not zip_path.with_suffix(".zip.tmp").exists()
        finally:
            src.chmod(0o644)


# ===================================================================
# Atomic write -- no partial output on failure
# ===================================================================
class TestAtomicWrite:
    """Verify the .zip.tmp pattern cleans up on failure."""

    @pytest.mark.edge
    def test_tmp_file_removed_on_write_error(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Simulate a write failure and confirm no .zip.tmp is left behind."""
        # Point at a file that doesn't exist → read will fail during archive creation
        ghost = tmp_path / "ghost.txt"

        collected = [
            CollectedFile(source_path=ghost, archive_path="f/sf/ghost.txt")
        ]
        zip_path = tmp_path / "output.zip"

        with pytest.raises(FileNotFoundError):
            create_archive(collected, zip_path, logger)

        assert not zip_path.exists(), "Partial .zip must not exist"
        assert not zip_path.with_suffix(".zip.tmp").exists(), ".zip.tmp must be cleaned up"


# ===================================================================
# run() error paths
# ===================================================================
class TestRunErrorPaths:
    """Verify the pipeline returns EXIT_ERROR on various failure modes."""

    @pytest.mark.edge
    def test_invalid_config_returns_error(self, tmp_path: Path) -> None:
        """Malformed YAML → EXIT_ERROR, no crash."""
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("not: valid: yaml: [", encoding="utf-8")

        assert run(bad_config) == EXIT_ERROR

    @pytest.mark.edge
    def test_nonexistent_config_returns_error(self, tmp_path: Path) -> None:
        assert run(tmp_path / "missing.yaml") == EXIT_ERROR

    @pytest.mark.edge
    def test_invalid_backup_name_returns_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "backup_name": "../escape/out",
                    "target_base_dir": str(tmp_path / "dest"),
                    "keep_last": 1,
                    "structure": {
                        "f": {
                            "sf": {
                                "source_dir": str(tmp_path),
                                "include": "*",
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        assert run(config_path) == EXIT_ERROR

    @pytest.mark.edge
    def test_collision_returns_error(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Destination collision during collect_files → EXIT_ERROR.

        YAML dict keys are unique, so a true collision cannot be expressed
        in a config file. We build the config programmatically with two
        SubfolderConfigs sharing the same folder/subfolder name (but
        pointing at different source dirs) and verify collect_files raises.
        """
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "same.txt").write_text("A")
        (dir_b / "same.txt").write_text("B")

        config = BackupConfig(
            backup_name="collision_test",
            target_base_dir=tmp_path / "dest",
            keep_last=0,
            subfolders=[
                SubfolderConfig("shared", "dup", dir_a, "*"),
                SubfolderConfig("shared", "dup", dir_b, "*"),
            ],
        )
        with pytest.raises(ValueError, match="[Cc]ollision"):
            collect_files(config, logger)


# ===================================================================
# Dangling symlinks
# ===================================================================
class TestDanglingSymlink:
    """A symlink whose target no longer exists must be skipped, not crash."""

    @pytest.mark.edge
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks may require elevated privileges on Windows",
    )
    def test_dangling_symlink_skipped(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        root = tmp_path / "source"
        root.mkdir()

        # Create a valid file and a dangling symlink
        (root / "valid.txt").write_text("ok")
        target = tmp_path / "will_be_deleted.txt"
        target.write_text("temporary")
        (root / "dangling.txt").symlink_to(target)
        target.unlink()  # Now the symlink dangles

        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="f",
                    subfolder_name="sf",
                    source_dir=root,
                    include="*.txt",
                ),
            ],
        )
        # Must not raise -- dangling link is skipped with a warning
        collected = collect_files(config, logger)
        names = [cf.archive_path for cf in collected]

        assert "f/sf/valid.txt" in names
        assert len(collected) == 1  # dangling link excluded


# ===================================================================
# Empty result set
# ===================================================================
class TestEmptyResultSet:
    """When no files match, no archive should be created."""

    @pytest.mark.edge
    def test_no_archive_on_empty_match(self, tmp_path: Path) -> None:
        """run() succeeds but produces no .zip when nothing matches."""
        src = tmp_path / "empty_src"
        src.mkdir()
        (src / "only.bin").write_bytes(b"\x00")  # won't match *.json

        dest = tmp_path / "dest"
        data = {
            "backup_name": "empty_test",
            "target_base_dir": str(dest),
            "keep_last": 0,
            "structure": {
                "f": {
                    "sf": {
                        "source_dir": str(src),
                        "include": "*.json",
                    },
                },
            },
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(data), encoding="utf-8")

        exit_code = run(config_path, dry_run=False)

        assert exit_code == EXIT_SUCCESS
        # Destination may or may not exist, but must contain no archives
        if dest.exists():
            assert list(dest.glob("*.zip")) == []


# ===================================================================
# Multiple subfolders in one run
# ===================================================================
class TestMultipleSubfolders:
    """Verify that multiple subfolders from different sources coexist correctly."""

    @pytest.mark.edge
    def test_three_subfolders_all_collected(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Three independent sources should all end up in the archive."""
        for name in ("src_a", "src_b", "src_c"):
            d = tmp_path / name
            d.mkdir()
            (d / f"{name}.dat").write_text(f"content of {name}")

        config = BackupConfig(
            backup_name="multi",
            target_base_dir=tmp_path / "dest",
            keep_last=0,
            subfolders=[
                SubfolderConfig("folder_a", "sub_a", tmp_path / "src_a", "*"),
                SubfolderConfig("folder_b", "sub_b", tmp_path / "src_b", "*"),
                SubfolderConfig("folder_c", "sub_c", tmp_path / "src_c", "*"),
            ],
        )
        collected = collect_files(config, logger)
        paths = sorted(cf.archive_path for cf in collected)

        assert paths == [
            "folder_a/sub_a/src_a.dat",
            "folder_b/sub_b/src_b.dat",
            "folder_c/sub_c/src_c.dat",
        ]
