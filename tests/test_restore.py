"""Tests for backup.restore -- the reverse pipeline."""

from __future__ import annotations

import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Dict

import pytest

from backup.archive import create_archive
from backup.collector import collect_files
from backup.models import BackupConfig, SubfolderConfig
from backup.restore import (
    RestoreEntry, _build_prefix_map, _check_path_traversal,
    _resolve_entry, detect_conflicts, execute_restore, plan_restore,
)


@pytest.fixture()
def restore_logger() -> logging.Logger:
    log = logging.getLogger("backup.test.restore")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.addHandler(logging.StreamHandler())
    return log


@pytest.fixture()
def backup_scenario(tmp_path: Path, restore_logger) -> Dict[str, object]:
    src_db = tmp_path / "orig" / "db"
    (src_db / "nested").mkdir(parents=True)
    (src_db / "data.db").write_bytes(b"SQLite content here")
    (src_db / "nested" / "deep.db").write_bytes(b"Nested DB data")
    src_logs = tmp_path / "orig" / "logs"
    src_logs.mkdir(parents=True)
    (src_logs / "server.log").write_text("2026-01-01 INFO started")

    bk_cfg = BackupConfig("test_restore", tmp_path / "bk", 0, subfolders=[
        SubfolderConfig("database", "sqlite", src_db, "*"),
        SubfolderConfig("logs", "app", src_logs, "*.log"),
    ])
    collected = collect_files(bk_cfg, restore_logger)
    zp = tmp_path / "bk" / "test.zip"
    zp.parent.mkdir(parents=True, exist_ok=True)
    create_archive(collected, zp, restore_logger)

    rst_db = tmp_path / "rst" / "db"
    rst_db.mkdir(parents=True)
    rst_logs = tmp_path / "rst" / "logs"
    rst_logs.mkdir(parents=True)
    rst_cfg = BackupConfig("test_restore", tmp_path / "bk", 0, subfolders=[
        SubfolderConfig("database", "sqlite", rst_db, "*"),
        SubfolderConfig("logs", "app", rst_logs, "*.log"),
    ])
    return dict(zip_path=zp, restore_config=rst_cfg, restore_db=rst_db,
                restore_logs=rst_logs, src_db=src_db, src_logs=src_logs)


class TestBuildPrefixMap:
    @pytest.mark.unit
    def test_basic(self, tmp_path):
        cfg = BackupConfig("x", tmp_path, 0, [
            SubfolderConfig("db", "sq", tmp_path / "a", "*"),
            SubfolderConfig("lg", "ap", tmp_path / "b", "*"),
        ])
        pm = _build_prefix_map(cfg)
        assert len(pm) == 2 and "db/sq" in pm and "lg/ap" in pm

    @pytest.mark.unit
    def test_empty(self, tmp_path):
        assert _build_prefix_map(BackupConfig("x", tmp_path, 0)) == {}


class TestResolveEntry:
    @pytest.mark.unit
    def test_basic(self, tmp_path):
        r = _resolve_entry("db/sq/f.db", {"db/sq": tmp_path})
        assert r and r[1] == "f.db"

    @pytest.mark.unit
    def test_nested(self, tmp_path):
        r = _resolve_entry("db/sq/a/b/c.db", {"db/sq": tmp_path})
        assert r and r[1] == "a/b/c.db"

    @pytest.mark.unit
    def test_unmapped(self, tmp_path):
        assert _resolve_entry("x/y/z", {"db/sq": tmp_path}) is None

    @pytest.mark.unit
    def test_too_short(self, tmp_path):
        assert _resolve_entry("db/sq", {"db/sq": tmp_path}) is None


class TestPathTraversal:
    @pytest.mark.unit
    def test_clean(self):
        _check_path_traversal("a/b/c")

    @pytest.mark.unit
    def test_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            _check_path_traversal("../etc/passwd")

    @pytest.mark.unit
    def test_dotdot_mid(self):
        with pytest.raises(ValueError, match="traversal"):
            _check_path_traversal("a/../../x")


class TestPlanRestore:
    @pytest.mark.restore
    def test_full(self, backup_scenario, restore_logger):
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger)
        assert sorted(e.archive_path for e in plan) == [
            "database/sqlite/data.db", "database/sqlite/nested/deep.db",
            "logs/app/server.log"]

    @pytest.mark.restore
    def test_selective(self, backup_scenario, restore_logger):
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger,
                            selected_prefixes=["database/sqlite"])
        assert len(plan) == 2

    @pytest.mark.restore
    def test_unknown_prefix(self, backup_scenario, restore_logger):
        with pytest.raises(ValueError, match="Unknown prefix"):
            plan_restore(backup_scenario["zip_path"],
                         backup_scenario["restore_config"], restore_logger,
                         selected_prefixes=["nope/nada"])

    @pytest.mark.restore
    def test_targets_correct(self, backup_scenario, restore_logger):
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger)
        db = str(backup_scenario["restore_db"].resolve())
        lg = str(backup_scenario["restore_logs"].resolve())
        for e in plan:
            rp = str(e.target_path.resolve())
            assert rp.startswith(db) or rp.startswith(lg)


class TestDetectConflicts:
    @pytest.mark.restore
    def test_clean(self, tmp_path):
        assert detect_conflicts([RestoreEntry("x", tmp_path / "nope")]) == []

    @pytest.mark.restore
    def test_existing(self, tmp_path):
        f = tmp_path / "e.txt"
        f.write_text("x")
        assert len(detect_conflicts([RestoreEntry("a", f)])) == 1


class TestExecuteRestore:
    @pytest.mark.restore
    def test_full_content(self, backup_scenario, restore_logger):
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger)
        assert execute_restore(backup_scenario["zip_path"], plan, restore_logger) == 3
        assert (backup_scenario["restore_db"] / "data.db").read_bytes() == b"SQLite content here"
        assert (backup_scenario["restore_db"] / "nested" / "deep.db").read_bytes() == b"Nested DB data"
        assert (backup_scenario["restore_logs"] / "server.log").read_text() == "2026-01-01 INFO started"

    @pytest.mark.restore
    def test_nested_dirs(self, backup_scenario, restore_logger):
        shutil.rmtree(backup_scenario["restore_db"])
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger,
                            selected_prefixes=["database/sqlite"])
        execute_restore(backup_scenario["zip_path"], plan, restore_logger)
        assert (backup_scenario["restore_db"] / "nested" / "deep.db").is_file()

    @pytest.mark.restore
    def test_conflict_blocks(self, backup_scenario, restore_logger):
        t = backup_scenario["restore_logs"] / "server.log"
        t.write_text("old")
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger,
                            selected_prefixes=["logs/app"])
        with pytest.raises(ValueError, match="already exist"):
            execute_restore(backup_scenario["zip_path"], plan, restore_logger)
        assert t.read_text() == "old"

    @pytest.mark.restore
    def test_overwrite(self, backup_scenario, restore_logger):
        t = backup_scenario["restore_logs"] / "server.log"
        t.write_text("old")
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger,
                            selected_prefixes=["logs/app"])
        assert execute_restore(backup_scenario["zip_path"], plan, restore_logger,
                               overwrite=True) == 1
        assert t.read_text() == "2026-01-01 INFO started"

    @pytest.mark.restore
    def test_dry_run(self, backup_scenario, restore_logger):
        shutil.rmtree(backup_scenario["restore_db"])
        shutil.rmtree(backup_scenario["restore_logs"])
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger)
        assert execute_restore(backup_scenario["zip_path"], plan, restore_logger,
                               dry_run=True) == 3
        assert not backup_scenario["restore_db"].exists()

    @pytest.mark.restore
    def test_empty_plan(self, tmp_path, restore_logger):
        assert execute_restore(tmp_path / "x.zip", [], restore_logger) == 0


class TestInvalidArchive:
    @pytest.mark.restore
    def test_missing(self, tmp_path, restore_logger):
        cfg = BackupConfig("x", tmp_path, 0, [SubfolderConfig("f", "s", tmp_path, "*")])
        with pytest.raises(ValueError, match="does not exist"):
            plan_restore(tmp_path / "ghost.zip", cfg, restore_logger)

    @pytest.mark.restore
    def test_non_zip(self, tmp_path, restore_logger):
        f = tmp_path / "fake.zip"
        f.write_text("nope")
        cfg = BackupConfig("x", tmp_path, 0, [SubfolderConfig("f", "s", tmp_path, "*")])
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            plan_restore(f, cfg, restore_logger)


class TestRestoreTraversal:
    @pytest.mark.restore
    def test_evil(self, tmp_path, restore_logger):
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(str(evil), "w") as zf:
            zf.writestr("db/sq/../../../etc/passwd", "x")
        cfg = BackupConfig("x", tmp_path, 0, [SubfolderConfig("db", "sq", tmp_path, "*")])
        with pytest.raises(ValueError, match="traversal"):
            plan_restore(evil, cfg, restore_logger)


class TestRoundTrip:
    @pytest.mark.restore
    def test_byte_match(self, backup_scenario, restore_logger):
        plan = plan_restore(backup_scenario["zip_path"],
                            backup_scenario["restore_config"], restore_logger)
        execute_restore(backup_scenario["zip_path"], plan, restore_logger)
        for sd, rd in [(backup_scenario["src_db"], backup_scenario["restore_db"]),
                       (backup_scenario["src_logs"], backup_scenario["restore_logs"])]:
            for sf in sd.rglob("*"):
                if sf.is_file():
                    rel = sf.relative_to(sd)
                    rf = rd / rel
                    assert rf.exists() and sf.read_bytes() == rf.read_bytes()

    @pytest.mark.restore
    def test_rotate_restore_latest(self, tmp_path, restore_logger):
        src = tmp_path / "src"
        src.mkdir()
        bk = tmp_path / "bk"
        bk.mkdir()
        for v in ("v1", "v2", "v3"):
            (src / "d.txt").write_text(v)
            cfg = BackupConfig("rot", bk, 2, [SubfolderConfig("d", "f", src, "*")])
            c = collect_files(cfg, restore_logger)
            zp = bk / f"rot_{time.strftime('%Y-%m-%d_%H-%M-%S')}.zip"
            create_archive(c, zp, restore_logger)
            from backup.rotation import rotate_backups
            rotate_backups(bk, "rot", 2, restore_logger)
            time.sleep(1.1)
        zips = sorted(bk.glob("rot_*.zip"))
        assert len(zips) == 2
        rst = tmp_path / "rst"
        rst.mkdir()
        rc = BackupConfig("rot", bk, 2, [SubfolderConfig("d", "f", rst, "*")])
        p = plan_restore(zips[-1], rc, restore_logger)
        execute_restore(zips[-1], p, restore_logger)
        assert (rst / "d.txt").read_text() == "v3"
