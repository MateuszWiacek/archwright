"""Integration tests -- filesystem interactions via tmp_path.

Covers:
  - File collection with include/exclude patterns
  - Recursive subdirectory scanning
  - Brace expansion in real collection
  - Symlink resolution (archives target content, not link)
  - Destination collision detection
  - Backup rotation (keep_last enforcement)
"""

from __future__ import annotations

import logging
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from backup.collector import collect_files, _walk_safe
from backup.archive import create_archive
from backup.models import BackupConfig, SubfolderConfig
from backup.rotation import rotate_backups


# ===================================================================
# collect_files -- include / exclude logic
# ===================================================================
class TestCollectFilesFiltering:
    """Verify glob-based include/exclude against a real directory tree."""

    @pytest.mark.integration
    def test_include_json_exclude_tmp(
        self, source_tree: Dict[str, Path], logger: logging.Logger
    ) -> None:
        """Only *.json files should be collected, minus *-tmp.json."""
        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="data",
                    subfolder_name="json",
                    source_dir=source_tree["root"],
                    include="*.json",
                    exclude="*-tmp.json",
                ),
            ],
        )
        collected = collect_files(config, logger)
        names = sorted(cf.archive_path for cf in collected)

        # data.json and nested/deep.json should match
        assert "data/json/data.json" in names
        assert "data/json/nested/deep.json" in names
        # cache-tmp.json excluded, readme.md unmatched
        assert not any("cache-tmp" in n for n in names)
        assert not any("readme" in n for n in names)
        assert len(collected) == 2

    @pytest.mark.integration
    def test_include_all(
        self, source_tree: Dict[str, Path], logger: logging.Logger
    ) -> None:
        """include='*' must collect every file in the tree."""
        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="everything",
                    subfolder_name="all",
                    source_dir=source_tree["root"],
                    include="*",
                ),
            ],
        )
        collected = collect_files(config, logger)
        # 4 files total: data.json, cache-tmp.json, readme.md, nested/deep.json
        assert len(collected) == 4

    @pytest.mark.integration
    def test_brace_expansion_integration(
        self, multi_source_tree: Dict[str, Path], logger: logging.Logger
    ) -> None:
        """Brace pattern *.{log,txt} must match both extensions."""
        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="logs",
                    subfolder_name="app",
                    source_dir=multi_source_tree["src_logs"],
                    include="*.{log,txt}",
                ),
            ],
        )
        collected = collect_files(config, logger)
        names = sorted(cf.archive_path for cf in collected)

        assert "logs/app/app.log" in names
        assert "logs/app/debug.txt" in names
        # binary.bin must not match
        assert not any("binary" in n for n in names)
        assert len(collected) == 2

    @pytest.mark.integration
    def test_no_files_match(
        self, source_tree: Dict[str, Path], logger: logging.Logger
    ) -> None:
        """A pattern that matches nothing should return an empty list -- not crash."""
        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="empty",
                    subfolder_name="nothing",
                    source_dir=source_tree["root"],
                    include="*.nonexistent_extension",
                ),
            ],
        )
        collected = collect_files(config, logger)
        assert collected == []

    @pytest.mark.integration
    def test_recursive_subdirectory_structure(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Files in nested subdirectories must preserve their relative path in the archive."""
        root = tmp_path / "deep_source"
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / "top.txt").write_text("top")
        (root / "a" / "mid.txt").write_text("mid")
        (root / "a" / "b" / "c" / "bottom.txt").write_text("bottom")

        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="deep",
                    subfolder_name="tree",
                    source_dir=root,
                    include="*.txt",
                ),
            ],
        )
        collected = collect_files(config, logger)
        paths = sorted(cf.archive_path for cf in collected)

        assert "deep/tree/top.txt" in paths
        assert "deep/tree/a/mid.txt" in paths
        assert "deep/tree/a/b/c/bottom.txt" in paths


# ===================================================================
# Symlink resolution
# ===================================================================
class TestSymlinkResolution:
    """Ensure symlinks are followed and real file content is archived."""

    @pytest.mark.integration
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks may require elevated privileges on Windows",
    )
    def test_symlink_resolves_to_target(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """A symlinked file should be collected with the *target's* real path."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real_file = real_dir / "original.json"
        real_file.write_text('{"real": true}')

        link_dir = tmp_path / "links"
        link_dir.mkdir()
        link_path = link_dir / "alias.json"
        link_path.symlink_to(real_file)

        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="linked",
                    subfolder_name="data",
                    source_dir=link_dir,
                    include="*.json",
                ),
            ],
        )
        collected = collect_files(config, logger)

        assert len(collected) == 1
        cf = collected[0]
        # source_path must be the *resolved real* file, not the symlink
        assert cf.source_path == real_file.resolve()
        assert cf.archive_path == "linked/data/alias.json"

    @pytest.mark.integration
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks may require elevated privileges on Windows",
    )
    def test_symlinked_content_in_archive(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """The ZIP must contain the *content* of the target file."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "target.txt").write_text("REAL_CONTENT")

        link_dir = tmp_path / "links"
        link_dir.mkdir()
        (link_dir / "pointer.txt").symlink_to(real_dir / "target.txt")

        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="f",
                    subfolder_name="sf",
                    source_dir=link_dir,
                    include="*.txt",
                ),
            ],
        )
        collected = collect_files(config, logger)

        # Write to a real ZIP and verify content
        zip_path = tmp_path / "test.zip"
        create_archive(collected, zip_path, logger)

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            assert zf.read("f/sf/pointer.txt").decode() == "REAL_CONTENT"


# ===================================================================
# Destination collision
# ===================================================================
class TestDestinationCollision:
    """Two source files mapping to the same archive path must be a hard error."""

    @pytest.mark.integration
    def test_collision_raises_valueerror(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Configure two subfolders with the same folder/subfolder name
        pointing at different source dirs containing identically-named files.
        """
        dir_a = tmp_path / "dir_a"
        dir_a.mkdir()
        (dir_a / "collision.txt").write_text("from A")

        dir_b = tmp_path / "dir_b"
        dir_b.mkdir()
        (dir_b / "collision.txt").write_text("from B")

        # Both map to "shared/stuff/collision.txt" inside the archive
        config = BackupConfig(
            backup_name="test",
            target_base_dir=Path("/unused"),
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="shared",
                    subfolder_name="stuff",
                    source_dir=dir_a,
                    include="*",
                ),
                SubfolderConfig(
                    folder_name="shared",
                    subfolder_name="stuff",
                    source_dir=dir_b,
                    include="*",
                ),
            ],
        )
        with pytest.raises(ValueError, match="Destination collision"):
            collect_files(config, logger)


# ===================================================================
# Backup rotation
# ===================================================================
class TestRotation:
    """Verify old archives are pruned and newest are kept."""

    @staticmethod
    def _create_dummy_backups(
        target: Path, name: str, count: int
    ) -> List[Path]:
        """Create *count* dummy .zip + .log pairs with sequential timestamps."""
        target.mkdir(parents=True, exist_ok=True)
        created_zips = []
        for i in range(count):
            ts = f"2026-01-{i + 1:02d}_00-00-00"
            zip_path = target / f"{name}_{ts}.zip"
            log_path = target / f"{name}_{ts}.log"
            zip_path.write_bytes(b"PK\x03\x04")  # minimal ZIP magic bytes
            log_path.write_text(f"log {i}")
            created_zips.append(zip_path)
        return created_zips

    @pytest.mark.integration
    def test_rotation_keeps_newest(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """With 6 backups and keep_last=3, the oldest 3 must be deleted."""
        target = tmp_path / "backups"
        zips = self._create_dummy_backups(target, "app", count=6)

        rotate_backups(target, "app", keep_last=3, logger=logger)

        remaining_zips = sorted(target.glob("app_*.zip"))
        remaining_logs = sorted(target.glob("app_*.log"))

        # Only the 3 newest survive
        assert len(remaining_zips) == 3
        assert len(remaining_logs) == 3
        assert remaining_zips == zips[3:]  # indices 3,4,5

    @pytest.mark.integration
    def test_rotation_deletes_matching_log(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Each deleted .zip must also remove its companion .log."""
        target = tmp_path / "backups"
        self._create_dummy_backups(target, "app", count=4)

        rotate_backups(target, "app", keep_last=2, logger=logger)

        # 2 zips + 2 logs remain = 4 files total
        all_files = list(target.iterdir())
        assert len(all_files) == 4

    @pytest.mark.integration
    def test_rotation_disabled_with_zero(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """keep_last=0 means unlimited retention -- nothing should be deleted."""
        target = tmp_path / "backups"
        self._create_dummy_backups(target, "app", count=10)

        rotate_backups(target, "app", keep_last=0, logger=logger)

        assert len(list(target.glob("app_*.zip"))) == 10

    @pytest.mark.integration
    def test_rotation_noop_when_under_limit(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """If existing count <= keep_last, nothing is deleted."""
        target = tmp_path / "backups"
        self._create_dummy_backups(target, "app", count=2)

        rotate_backups(target, "app", keep_last=5, logger=logger)

        assert len(list(target.glob("app_*.zip"))) == 2

    @pytest.mark.integration
    def test_rotation_ignores_other_backup_names(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Rotation must only touch archives matching the backup_name prefix."""
        target = tmp_path / "backups"
        self._create_dummy_backups(target, "app", count=5)
        self._create_dummy_backups(target, "other", count=5)

        rotate_backups(target, "app", keep_last=2, logger=logger)

        # "app" pruned to 2, "other" untouched at 5
        assert len(list(target.glob("app_*.zip"))) == 2
        assert len(list(target.glob("other_*.zip"))) == 5

    @pytest.mark.integration
    def test_rotation_missing_log_is_tolerated(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """If a .log companion is missing, rotation should still delete the .zip."""
        target = tmp_path / "backups"
        target.mkdir()

        for i in range(4):
            ts = f"2026-01-{i + 1:02d}_00-00-00"
            (target / f"app_{ts}.zip").write_bytes(b"PK")
            # Deliberately skip .log for the first two
            if i >= 2:
                (target / f"app_{ts}.log").write_text("log")

        rotate_backups(target, "app", keep_last=2, logger=logger)

        assert len(list(target.glob("app_*.zip"))) == 2


# ===================================================================
# _walk_safe -- symlink cycle detection
# ===================================================================
class TestWalkSafeSymlinkCycle:
    """Verify the walker doesn't infinite-loop on circular symlinks."""

    @pytest.mark.integration
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlink cycle testing requires Unix-like filesystem",
    )
    def test_cycle_is_detected_and_skipped(
        self, tmp_path: Path, logger: logging.Logger
    ) -> None:
        """Create a directory symlink cycle: a/link -> a. Walker must terminate."""
        root = tmp_path / "cycle_root"
        root.mkdir()
        (root / "file.txt").write_text("safe")

        # Create a circular symlink: cycle_root/loop -> cycle_root
        (root / "loop").symlink_to(root)

        files = list(_walk_safe(root, logger=logger))
        # Must find file.txt exactly once, not loop forever
        names = [f.name for f in files]
        assert names.count("file.txt") == 1
