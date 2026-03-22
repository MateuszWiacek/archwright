"""Tests for config extensions and validation-related CLI helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import pytest
import yaml

from backup.cli import parse_args, run_validate
from backup.config import load_config
from backup.constants import (
    DEFAULT_DUMP_TIMEOUT,
    DEFAULT_HOOK_TIMEOUT,
    EXIT_ERROR,
    EXIT_SUCCESS,
)
from backup.db_dump import get_provider, run_dumps
from backup.models import BackupConfig, DatabaseConfig, SubfolderConfig


class TestRuntimeTuningParsing:
    """Verify optional log_level, hook_timeout, dump_timeout parsing."""

    @pytest.mark.unit
    def test_defaults_when_omitted(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
        }
        config = load_config(write_yaml(data))
        assert config.log_level == "INFO"
        assert config.hook_timeout == DEFAULT_HOOK_TIMEOUT
        assert config.dump_timeout == DEFAULT_DUMP_TIMEOUT

    @pytest.mark.unit
    def test_custom_values(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "log_level": "debug",
            "hook_timeout": 60,
            "dump_timeout": 7200,
        }
        config = load_config(write_yaml(data))
        assert config.log_level == "DEBUG"
        assert config.hook_timeout == 60
        assert config.dump_timeout == 7200

    @pytest.mark.unit
    def test_log_level_case_insensitive(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "log_level": "Warning",
        }
        config = load_config(write_yaml(data))
        assert config.log_level == "WARNING"

    @pytest.mark.unit
    def test_invalid_log_level(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "log_level": "TRACE",
        }
        with pytest.raises(ValueError, match="log_level"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_log_level_not_string(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "log_level": 42,
        }
        with pytest.raises(ValueError, match="log_level.*string"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_hook_timeout_not_int(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "hook_timeout": "fast",
        }
        with pytest.raises(ValueError, match="hook_timeout.*integer"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_hook_timeout_zero(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "hook_timeout": 0,
        }
        with pytest.raises(ValueError, match="hook_timeout.*> 0"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_dump_timeout_negative(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "dump_timeout": -1,
        }
        with pytest.raises(ValueError, match="dump_timeout.*> 0"):
            load_config(write_yaml(data))

    @pytest.mark.unit
    def test_hook_timeout_bool_rejected(self, write_yaml) -> None:
        data = {
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": 3,
            "structure": {"f": {"sf": {"source_dir": "/tmp", "include": "*"}}},
            "hook_timeout": True,
        }
        with pytest.raises(ValueError, match="hook_timeout.*integer"):
            load_config(write_yaml(data))


class TestVerboseQuietFlags:
    @pytest.mark.e2e
    def test_verbose_flag(self, tmp_path: Path) -> None:
        args = parse_args(
            ["backup", "--config", str(tmp_path / "c.yaml"), "--verbose"]
        )
        assert args.verbose is True
        assert args.quiet is False

    @pytest.mark.e2e
    def test_quiet_flag(self, tmp_path: Path) -> None:
        args = parse_args(
            ["backup", "--config", str(tmp_path / "c.yaml"), "--quiet"]
        )
        assert args.quiet is True
        assert args.verbose is False

    @pytest.mark.e2e
    def test_verbose_short_flag(self, tmp_path: Path) -> None:
        args = parse_args(
            ["backup", "--config", str(tmp_path / "c.yaml"), "-v"]
        )
        assert args.verbose is True

    @pytest.mark.e2e
    def test_quiet_short_flag(self, tmp_path: Path) -> None:
        args = parse_args(
            ["backup", "--config", str(tmp_path / "c.yaml"), "-q"]
        )
        assert args.quiet is True

    @pytest.mark.e2e
    def test_verbose_quiet_mutual_exclusion(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["backup", "--config", "c.yaml", "--verbose", "--quiet"])

    @pytest.mark.e2e
    def test_restore_has_verbose_quiet(self, tmp_path: Path) -> None:
        args = parse_args([
            "restore", "--config", str(tmp_path / "c.yaml"),
            "--archive", str(tmp_path / "a.zip"), "--verbose",
        ])
        assert args.verbose is True

    @pytest.mark.e2e
    def test_list_has_verbose_quiet(self, tmp_path: Path) -> None:
        args = parse_args(
            ["list", "--config", str(tmp_path / "c.yaml"), "-q"]
        )
        assert args.quiet is True

    @pytest.mark.e2e
    def test_validate_has_verbose_quiet(self, tmp_path: Path) -> None:
        args = parse_args(
            ["validate", "--config", str(tmp_path / "c.yaml"), "-v"]
        )
        assert args.verbose is True


class TestValidateSubcommand:
    @pytest.mark.e2e
    def test_parse_validate(self, tmp_path: Path) -> None:
        args = parse_args(["validate", "--config", str(tmp_path / "c.yaml")])
        assert args.command == "validate"
        assert args.config == tmp_path / "c.yaml"

    @pytest.mark.e2e
    def test_validate_valid_config(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({
                "backup_name": "valid_test",
                "target_base_dir": str(multi_source_tree["dest"]),
                "keep_last": 3,
                "structure": {
                    "data": {
                        "json_files": {
                            "source_dir": str(multi_source_tree["src_json"]),
                            "include": "*.json",
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        assert run_validate(config_path) == EXIT_SUCCESS

    @pytest.mark.e2e
    def test_validate_invalid_config(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("backup_name: test\n", encoding="utf-8")
        assert run_validate(bad) == EXIT_ERROR

    @pytest.mark.e2e
    def test_validate_missing_source(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({
                "backup_name": "test",
                "target_base_dir": str(tmp_path / "dest"),
                "keep_last": 0,
                "structure": {
                    "f": {
                        "sf": {
                            "source_dir": str(tmp_path / "nonexistent"),
                            "include": "*",
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        assert run_validate(config_path) == EXIT_ERROR

    @pytest.mark.e2e
    def test_validate_does_not_create_backup(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        dest = multi_source_tree["dest"]
        config_path.write_text(
            yaml.dump({
                "backup_name": "no_backup",
                "target_base_dir": str(dest),
                "keep_last": 3,
                "structure": {
                    "data": {
                        "files": {
                            "source_dir": str(multi_source_tree["src_json"]),
                            "include": "*.json",
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        run_validate(config_path)
        if dest.exists():
            assert list(dest.glob("*.zip")) == []

    @pytest.mark.e2e
    def test_validate_target_base_dir_file_fails(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        target_file = tmp_path / "not_a_dir"
        target_file.write_text("x", encoding="utf-8")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({
                "backup_name": "bad_target",
                "target_base_dir": str(target_file),
                "keep_last": 3,
                "structure": {
                    "data": {
                        "files": {
                            "source_dir": str(multi_source_tree["src_json"]),
                            "include": "*.json",
                        },
                    },
                },
            }),
            encoding="utf-8",
        )
        assert run_validate(config_path) == EXIT_ERROR

    @pytest.mark.e2e
    def test_validate_missing_sqlite_file_fails(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({
                "backup_name": "missing_sqlite",
                "target_base_dir": str(multi_source_tree["dest"]),
                "keep_last": 3,
                "structure": {
                    "data": {
                        "files": {
                            "source_dir": str(multi_source_tree["src_json"]),
                            "include": "*.json",
                        },
                    },
                },
                "databases": {
                    "app_db": {
                        "provider": "sqlite",
                        "db_path": str(tmp_path / "missing.sqlite3"),
                    },
                },
            }),
            encoding="utf-8",
        )

        with patch("backup.db_dump.shutil.which", return_value="/usr/bin/sqlite3"):
            assert run_validate(config_path) == EXIT_ERROR

    @pytest.mark.e2e
    def test_validate_missing_dump_tool_fails(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        db_path = tmp_path / "app.sqlite3"
        db_path.write_text("not really sqlite", encoding="utf-8")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({
                "backup_name": "missing_tool",
                "target_base_dir": str(multi_source_tree["dest"]),
                "keep_last": 3,
                "structure": {
                    "data": {
                        "files": {
                            "source_dir": str(multi_source_tree["src_json"]),
                            "include": "*.json",
                        },
                    },
                },
                "databases": {
                    "app_db": {
                        "provider": "sqlite",
                        "db_path": str(db_path),
                        "sqlite3_path": "/nonexistent/sqlite3",
                    },
                },
            }),
            encoding="utf-8",
        )

        with patch("backup.db_dump.shutil.which", return_value=None):
            assert run_validate(config_path) == EXIT_ERROR

    @pytest.mark.e2e
    def test_validate_docker_container_not_found_fails(
        self, multi_source_tree: Dict[str, Path], tmp_path: Path
    ) -> None:
        """validate must fail if the docker container does not exist."""
        # Mock docker binary that succeeds on 'which' but fails on 'inspect'
        mock_docker = tmp_path / "mock_docker"
        mock_docker.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "inspect" ]; then\n'
            '    echo "Error: No such container: ghost" >&2\n'
            '    exit 1\n'
            '  fi\n'
            'done\n'
            'exit 0\n'
        )
        import stat
        mock_docker.chmod(mock_docker.stat().st_mode | stat.S_IEXEC)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({
                "backup_name": "docker_validate",
                "target_base_dir": str(multi_source_tree["dest"]),
                "keep_last": 3,
                "structure": {
                    "data": {
                        "files": {
                            "source_dir": str(multi_source_tree["src_json"]),
                            "include": "*.json",
                        },
                    },
                },
                "databases": {
                    "app_db": {
                        "provider": "docker_postgres",
                        "container": "ghost",
                        "dbname": "testdb",
                        "docker_path": str(mock_docker),
                    },
                },
            }),
            encoding="utf-8",
        )

        assert run_validate(config_path) == EXIT_ERROR


class TestDumpToolErrorMessage:
    @pytest.mark.unit
    def test_sqlite_error_mentions_sqlite3(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        config = BackupConfig(
            "test", tmp_path / "out", 0,
            subfolders=[SubfolderConfig("d", "s", src, "*")],
            databases=[DatabaseConfig(
                name="mydb",
                provider="sqlite",
                archive_prefix="databases/mydb",
                db_path=str(tmp_path / "app.db"),
                sqlite3_path="/nonexistent/sqlite3",
            )],
        )
        logger = logging.getLogger("backup.test.errmsg")
        logger.handlers.clear()
        logger.addHandler(logging.StreamHandler())

        with pytest.raises(RuntimeError, match="sqlite3.*not on PATH"):
            run_dumps(config, logger)

    @pytest.mark.unit
    def test_postgres_error_mentions_pg_dump(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        config = BackupConfig(
            "test", tmp_path / "out", 0,
            subfolders=[SubfolderConfig("d", "s", src, "*")],
            databases=[DatabaseConfig(
                name="mydb",
                provider="postgres",
                archive_prefix="databases/mydb",
                dbname="testdb",
                pg_dump_path="/nonexistent/pg_dump",
            )],
        )
        logger = logging.getLogger("backup.test.errmsg")
        logger.handlers.clear()
        logger.addHandler(logging.StreamHandler())

        with pytest.raises(RuntimeError, match="pg_dump.*not on PATH"):
            run_dumps(config, logger)


    @pytest.mark.unit
    def test_docker_postgres_error_mentions_docker(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        config = BackupConfig(
            "test", tmp_path / "out", 0,
            subfolders=[SubfolderConfig("d", "s", src, "*")],
            databases=[DatabaseConfig(
                name="mydb",
                provider="docker_postgres",
                archive_prefix="databases/mydb",
                container="test_container",
                dbname="testdb",
                docker_path="/nonexistent/docker",
            )],
        )
        logger = logging.getLogger("backup.test.errmsg")
        logger.handlers.clear()
        logger.addHandler(logging.StreamHandler())

        with pytest.raises(RuntimeError, match="docker.*not on PATH"):
            run_dumps(config, logger)


class TestTimeoutPropagation:
    @pytest.mark.unit
    def test_provider_receives_custom_timeouts(self) -> None:
        cfg = DatabaseConfig(
            name="x", provider="postgres", archive_prefix="db/x",
            dbname="mydb",
        )
        logger = logging.getLogger("backup.test.timeout")
        provider = get_provider(cfg, logger, hook_timeout=60, dump_timeout=120)
        assert provider.hook_timeout == 60
        assert provider.dump_timeout == 120

    @pytest.mark.unit
    def test_provider_default_timeouts(self) -> None:
        cfg = DatabaseConfig(
            name="x", provider="postgres", archive_prefix="db/x",
            dbname="mydb",
        )
        logger = logging.getLogger("backup.test.timeout")
        provider = get_provider(cfg, logger)
        assert provider.hook_timeout == 300
        assert provider.dump_timeout == 3600
