"""Shared fixtures for the backup test suite.

Every fixture that builds filesystem structures uses pathlib exclusively --
no os.walk, no os.path, no raw string concatenation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict

import pytest
import yaml

from backup.models import BackupConfig


# ---------------------------------------------------------------------------
# Logger fixture -- lightweight, no file handler
# ---------------------------------------------------------------------------
@pytest.fixture()
def logger() -> logging.Logger:
    """Return a fresh logger that writes to stdout only (for capsys capture)."""
    log = logging.getLogger("backup.test")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    return log


# ---------------------------------------------------------------------------
# Filesystem tree builders
# ---------------------------------------------------------------------------
@pytest.fixture()
def source_tree(tmp_path: Path) -> Dict[str, Path]:
    """Create a representative source directory tree and return key paths.

    Layout::

        source/
        ├── data.json
        ├── cache-tmp.json   (should be excluded)
        ├── readme.md        (unmatched by *.json)
        └── nested/
            └── deep.json
    """
    root = tmp_path / "source"
    nested = root / "nested"
    nested.mkdir(parents=True)

    (root / "data.json").write_text('{"key": "value"}')
    (root / "cache-tmp.json").write_text('{"tmp": true}')
    (root / "readme.md").write_text("# README")
    (nested / "deep.json").write_text('{"deep": true}')

    return {
        "root": root,
        "nested": nested,
        "data_json": root / "data.json",
        "cache_tmp": root / "cache-tmp.json",
        "readme": root / "readme.md",
        "deep_json": nested / "deep.json",
    }


@pytest.fixture()
def multi_source_tree(tmp_path: Path) -> Dict[str, Path]:
    """Create two independent source directories for multi-subfolder configs.

    Layout::

        src_json/
        ├── data.json
        └── cache-tmp.json

        src_logs/
        ├── app.log
        ├── debug.txt
        └── binary.bin   (unmatched by *.{log,txt})
    """
    src_json = tmp_path / "src_json"
    src_json.mkdir()
    (src_json / "data.json").write_text('{"ok": true}')
    (src_json / "cache-tmp.json").write_text('{"tmp": true}')

    src_logs = tmp_path / "src_logs"
    src_logs.mkdir()
    (src_logs / "app.log").write_text("2026-01-01 INFO start")
    (src_logs / "debug.txt").write_text("debug output")
    (src_logs / "binary.bin").write_bytes(b"\x00\x01\x02")

    return {
        "src_json": src_json,
        "src_logs": src_logs,
        "dest": tmp_path / "dest",
    }


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------
@pytest.fixture()
def make_config() -> Callable[..., BackupConfig]:
    """Factory fixture: build a BackupConfig with sensible defaults.

    Caller can override any field via keyword arguments.
    """

    def _factory(
        backup_name: str = "test_backup",
        target_base_dir: Path = Path("/tmp/fallback"),
        keep_last: int = 3,
        subfolders: list | None = None,
    ) -> BackupConfig:
        return BackupConfig(
            backup_name=backup_name,
            target_base_dir=target_base_dir,
            keep_last=keep_last,
            subfolders=subfolders or [],
        )

    return _factory


@pytest.fixture()
def write_yaml(tmp_path: Path) -> Callable[..., Path]:
    """Factory fixture: write a dict as YAML and return the file path."""

    def _factory(data: dict, filename: str = "config.yaml") -> Path:
        path = tmp_path / filename
        path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        return path

    return _factory


@pytest.fixture()
def valid_yaml_data(multi_source_tree: Dict[str, Path]) -> dict:
    """Return a valid config dict pointing at real directories from multi_source_tree."""
    return {
        "backup_name": "test_backup",
        "target_base_dir": str(multi_source_tree["dest"]),
        "keep_last": 3,
        "structure": {
            "data": {
                "json_files": {
                    "source_dir": str(multi_source_tree["src_json"]),
                    "include": "*.json",
                    "exclude": "*-tmp.json",
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
