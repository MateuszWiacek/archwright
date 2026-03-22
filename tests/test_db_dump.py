"""Tests for backup.db_dump -- database dump module.

Since pg_dump is not available in the test environment, we use a mock
script that simulates pg_dump by writing a predictable file.

Covers:
  - Config parsing: valid database config, missing provider, bad port,
    stop without start, unsupported provider
  - Provider registry: get_provider for known/unknown providers
  - PostgresProvider.detect(): available vs missing pg_dump
  - PostgresProvider.dump(): produces file via mock pg_dump
  - SqliteProvider.dump(): quotes .backup output paths safely
  - run_dumps(): full pipeline with mock, dry-run, no databases
  - Service control: pre_backup/post_backup opt-in only
  - Staging directory cleanup
  - Integration with archive: db dumps appear in ZIP
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from backup.archive import create_archive
from backup.cli import run
from backup.collector import collect_files
from backup.config import load_config
from backup.constants import EXIT_ERROR, EXIT_SUCCESS
from backup.db_dump import (
    DockerPostgresProvider,
    PostgresProvider,
    SqliteProvider,
    get_provider,
    run_dumps,
)
from backup.models import BackupConfig, DatabaseConfig, SubfolderConfig


@pytest.fixture()
def db_logger() -> logging.Logger:
    log = logging.getLogger("backup.test.db_dump")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.addHandler(logging.StreamHandler())
    return log


@pytest.fixture()
def mock_pg_dump(tmp_path: Path) -> Path:
    """Create a shell script that mimics pg_dump by writing a dump file.

    It reads the --file argument and writes predictable content.
    """
    script = tmp_path / "mock_pg_dump"
    script.write_text(
        '#!/bin/sh\n'
        '# Mock pg_dump -- find --file argument and write to it\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --file) OUTPUT="$2"; shift 2;;\n'
        '    *) shift;;\n'
        '  esac\n'
        'done\n'
        'if [ -n "$OUTPUT" ]; then\n'
        '  echo "MOCK_PG_DUMP_OUTPUT" > "$OUTPUT"\n'
        '  exit 0\n'
        'fi\n'
        'exit 1\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture()
def pg_db_config(mock_pg_dump: Path) -> DatabaseConfig:
    """A DatabaseConfig pointing at the mock pg_dump."""
    return DatabaseConfig(
        name="test_pg",
        provider="postgres",
        archive_prefix="databases/test_pg",
        host="localhost",
        port=5432,
        user="testuser",
        password="secret",
        dbname="mydb",
        pg_dump_path=str(mock_pg_dump),
    )


@pytest.fixture()
def pg_backup_config(
    tmp_path: Path, pg_db_config: DatabaseConfig
) -> BackupConfig:
    """BackupConfig with one subfolder + one database."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "file.txt").write_text("hello")
    return BackupConfig(
        backup_name="db_test",
        target_base_dir=tmp_path / "backups",
        keep_last=0,
        subfolders=[SubfolderConfig("data", "files", src, "*")],
        databases=[pg_db_config],
    )


# ===================================================================
# Config parsing
# ===================================================================
class TestDatabaseConfigParsing:
    @pytest.mark.unit
    def test_valid_database_config(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path / "dest"),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {
                "main_pg": {
                    "provider": "postgres",
                    "host": "db.local",
                    "port": 5433,
                    "user": "admin",
                    "password": "s3cret",
                    "dbname": "production",
                    "extra_args": ["--no-owner", "--clean"],
                },
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        config = load_config(cfg_path)

        assert len(config.databases) == 1
        db = config.databases[0]
        assert db.name == "main_pg"
        assert db.provider == "postgres"
        assert db.host == "db.local"
        assert db.port == 5433
        assert db.user == "admin"
        assert db.password == "s3cret"
        assert db.dbname == "production"
        assert db.extra_args == ["--no-owner", "--clean"]
        assert db.archive_prefix == "databases/main_pg"
        assert db.stop_command is None
        assert db.start_command is None

    @pytest.mark.unit
    def test_unsupported_provider(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {"db1": {"provider": "oracle"}},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="Unsupported provider"):
            load_config(cfg_path)

    @pytest.mark.unit
    def test_missing_provider(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {"db1": {"host": "localhost"}},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="provider"):
            load_config(cfg_path)

    @pytest.mark.unit
    def test_database_name_rejects_path_separators(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {"../escape": {"provider": "postgres", "dbname": "main"}},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="Database name.*path separators"):
            load_config(cfg_path)

    @pytest.mark.unit
    def test_bad_port_type(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {
                "db1": {
                    "provider": "postgres",
                    "dbname": "main",
                    "port": "abc",
                },
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="port.*integer"):
            load_config(cfg_path)

    @pytest.mark.unit
    def test_missing_dbname_rejected_for_postgres(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {"db1": {"provider": "postgres"}},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="dbname"):
            load_config(cfg_path)

    @pytest.mark.unit
    def test_stop_without_start(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {"db1": {
                "provider": "postgres",
                "dbname": "main",
                "stop_command": "systemctl stop pg",
            }},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="stop_command.*start_command"):
            load_config(cfg_path)

    @pytest.mark.unit
    def test_no_databases_section(self, tmp_path: Path) -> None:
        """Config without databases section is valid -- field defaults to []."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        config = load_config(cfg_path)
        assert config.databases == []

    @pytest.mark.unit
    def test_service_control_opt_in(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "test",
            "target_base_dir": str(tmp_path),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {"db1": {
                "provider": "postgres",
                "dbname": "main",
                "stop_command": "systemctl stop pg",
                "start_command": "systemctl start pg",
            }},
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        config = load_config(cfg_path)
        db = config.databases[0]
        assert db.stop_command == "systemctl stop pg"
        assert db.start_command == "systemctl start pg"


# ===================================================================
# Provider registry
# ===================================================================
class TestProviderRegistry:
    @pytest.mark.unit
    def test_postgres_provider(self, pg_db_config, db_logger):
        p = get_provider(pg_db_config, db_logger)
        assert isinstance(p, PostgresProvider)

    @pytest.mark.unit
    def test_unknown_provider(self, db_logger):
        cfg = DatabaseConfig(
            name="x", provider="oracle", archive_prefix="db/x",
        )
        with pytest.raises(ValueError, match="No provider"):
            get_provider(cfg, db_logger)


# ===================================================================
# PostgresProvider
# ===================================================================
class TestPostgresProvider:
    @pytest.mark.integration
    def test_detect_with_mock(self, pg_db_config, db_logger):
        p = PostgresProvider(pg_db_config, db_logger)
        assert p.detect() is True

    @pytest.mark.integration
    def test_detect_missing(self, db_logger):
        cfg = DatabaseConfig(
            name="x", provider="postgres", archive_prefix="db/x",
            pg_dump_path="/nonexistent/pg_dump",
        )
        p = PostgresProvider(cfg, db_logger)
        assert p.detect() is False

    @pytest.mark.integration
    def test_dump_produces_file(self, pg_db_config, tmp_path, db_logger):
        p = PostgresProvider(pg_db_config, db_logger)
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        files = p.dump(output_dir)
        assert len(files) == 1
        assert files[0].exists()
        assert files[0].read_text().strip() == "MOCK_PG_DUMP_OUTPUT"

    @pytest.mark.integration
    def test_dump_filename_format(self, pg_db_config, tmp_path, db_logger):
        p = PostgresProvider(pg_db_config, db_logger)
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        files = p.dump(output_dir)
        name = files[0].name
        assert name.startswith("test_pg_mydb_")
        assert name.endswith(".dump")

    @pytest.mark.integration
    def test_validate_uses_password_for_pg_isready(
        self, tmp_path: Path, db_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pg_isready = tmp_path / "pg_isready"
        pg_isready.write_text(
            '#!/bin/sh\n'
            'if [ "$PGPASSWORD" = "secret" ]; then\n'
            '  exit 0\n'
            'fi\n'
            'echo "password required"\n'
            'exit 1\n'
        )
        pg_isready.chmod(pg_isready.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

        cfg = DatabaseConfig(
            name="pg_validate",
            provider="postgres",
            archive_prefix="databases/pg_validate",
            host="db.local",
            port=5432,
            user="app",
            password="secret",
            dbname="appdb",
            pg_dump_path="/nonexistent/pg_dump",
        )

        provider = PostgresProvider(cfg, db_logger)
        provider.validate()


# ===================================================================
# SqliteProvider
# ===================================================================
class TestSqliteProvider:
    @pytest.mark.integration
    def test_dump_quotes_output_path_with_spaces(
        self, tmp_path: Path, db_logger: logging.Logger
    ) -> None:
        source_db = tmp_path / "app.db"
        source_db.write_bytes(b"sqlite-placeholder")
        output_dir = tmp_path / "with space"
        output_dir.mkdir()

        provider = SqliteProvider(
            DatabaseConfig(
                name="sqlite_main",
                provider="sqlite",
                archive_prefix="databases/sqlite_main",
                db_path=str(source_db),
                sqlite3_path="sqlite3",
            ),
            db_logger,
        )

        def fake_run(cmd, capture_output, text, timeout):
            assert cmd[2].startswith('.backup "')
            assert cmd[2].endswith('"')
            quoted_output = cmd[2][len('.backup "'):-1]
            Path(quoted_output).write_bytes(b"SQLITE_BACKUP")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("backup.db_dump.subprocess.run", side_effect=fake_run):
            files = provider.dump(output_dir)

        assert len(files) == 1
        assert files[0].exists()
        assert files[0].read_bytes() == b"SQLITE_BACKUP"


# ===================================================================
# run_dumps
# ===================================================================
class TestRunDumps:
    @pytest.mark.integration
    def test_no_databases(self, tmp_path, db_logger):
        cfg = BackupConfig("x", tmp_path, 0,
                           [SubfolderConfig("d", "s", tmp_path, "*")])
        collected, staging = run_dumps(cfg, db_logger)
        assert collected == []
        assert staging is None

    @pytest.mark.integration
    def test_dry_run(self, pg_backup_config, db_logger):
        collected, staging = run_dumps(pg_backup_config, db_logger, dry_run=True)
        assert len(collected) == 1
        assert collected[0].archive_path.startswith("databases/test_pg/")
        assert not collected[0].source_path.exists()
        assert staging is None
        assert not pg_backup_config.target_base_dir.exists()

    @pytest.mark.integration
    def test_full_dump(self, pg_backup_config, db_logger):
        collected, staging = run_dumps(pg_backup_config, db_logger)
        assert len(collected) == 1
        assert collected[0].archive_path.startswith("databases/test_pg/")
        assert collected[0].source_path.exists()
        assert collected[0].source_path.read_text().strip() == "MOCK_PG_DUMP_OUTPUT"
        assert staging is not None

    @pytest.mark.integration
    def test_staging_cleanup_by_caller(self, pg_backup_config, db_logger):
        _, staging = run_dumps(pg_backup_config, db_logger)
        assert staging.exists()
        import shutil
        shutil.rmtree(staging)
        assert not staging.exists()

    @pytest.mark.integration
    def test_failure_cleans_staging_dir(self, pg_backup_config, db_logger):
        with patch.object(PostgresProvider, "dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                run_dumps(pg_backup_config, db_logger)

        staging_dirs = list(pg_backup_config.target_base_dir.glob(".db_staging_*"))
        assert staging_dirs == []


# ===================================================================
# Integration: db dumps in archive
# ===================================================================
class TestDbDumpInArchive:
    @pytest.mark.integration
    def test_db_dump_appears_in_zip(self, pg_backup_config, tmp_path, db_logger):
        """Run full pipeline: collect files + db dump → archive → verify ZIP."""
        import shutil as _shutil
        cfg = pg_backup_config
        cfg.target_base_dir.mkdir(parents=True, exist_ok=True)

        # Collect files
        file_collected = collect_files(cfg, db_logger)

        # Run dumps
        db_collected, staging = run_dumps(cfg, db_logger)

        all_collected = file_collected + db_collected

        zp = cfg.target_base_dir / "test.zip"
        create_archive(all_collected, zp, db_logger)

        if staging and staging.exists():
            _shutil.rmtree(staging)

        with zipfile.ZipFile(str(zp), "r") as zf:
            names = zf.namelist()
            # File from structure
            assert "data/files/file.txt" in names
            # DB dump
            db_entries = [n for n in names if n.startswith("databases/test_pg/")]
            assert len(db_entries) == 1
            content = zf.read(db_entries[0]).decode().strip()
            assert content == "MOCK_PG_DUMP_OUTPUT"


# ===================================================================
# Service control
# ===================================================================
class TestServiceControl:
    @pytest.mark.integration
    def test_no_service_control_by_default(self, pg_db_config, db_logger):
        """Pre/post backup must be no-ops without explicit stop/start."""
        p = PostgresProvider(pg_db_config, db_logger)
        # Should not raise
        p.pre_backup()
        p.post_backup()

    @pytest.mark.integration
    def test_service_control_runs_commands(self, mock_pg_dump, tmp_path, db_logger):
        marker_stop = tmp_path / "stopped"
        marker_start = tmp_path / "started"
        cfg = DatabaseConfig(
            name="svc_test",
            provider="postgres",
            archive_prefix="databases/svc_test",
            pg_dump_path=str(mock_pg_dump),
            dbname="mydb",
            stop_command=f"touch {marker_stop}",
            start_command=f"touch {marker_start}",
        )
        p = PostgresProvider(cfg, db_logger)
        p.pre_backup()
        assert marker_stop.exists()
        p.post_backup()
        assert marker_start.exists()


# ===================================================================
# CLI integration
# ===================================================================
class TestDbDumpCliIntegration:
    @pytest.mark.e2e
    def test_dry_run_with_databases_creates_no_target_dir(
        self, tmp_path: Path, mock_pg_dump: Path
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        dest = tmp_path / "backups"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "backup_name": "db_dry_run",
                    "target_base_dir": str(dest),
                    "keep_last": 0,
                    "structure": {
                        "data": {
                            "files": {
                                "source_dir": str(src),
                                "include": "*",
                            },
                        },
                    },
                    "databases": {
                        "main": {
                            "provider": "postgres",
                            "dbname": "app",
                            "pg_dump_path": str(mock_pg_dump),
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        assert run(config_path, dry_run=True) == EXIT_SUCCESS
        assert not dest.exists()

    @pytest.mark.e2e
    def test_run_cleans_staging_if_collection_fails(
        self, tmp_path: Path, mock_pg_dump: Path
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        dest = tmp_path / "backups"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump(
                {
                    "backup_name": "db_cleanup",
                    "target_base_dir": str(dest),
                    "keep_last": 0,
                    "structure": {
                        "data": {
                            "files": {
                                "source_dir": str(src),
                                "include": "*",
                            },
                        },
                    },
                    "databases": {
                        "main": {
                            "provider": "postgres",
                            "dbname": "app",
                            "pg_dump_path": str(mock_pg_dump),
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        with patch("backup.cli.collect_files", side_effect=ValueError("synthetic failure")):
            assert run(config_path, dry_run=False) == EXIT_ERROR

        assert list(dest.glob(".db_staging_*")) == []


# ===================================================================
# Docker exec PostgreSQL provider
# ===================================================================
class TestDockerPostgresProvider:
    """Tests for the docker_postgres provider.

    Since docker is not necessarily available in the test environment,
    we use a mock script that simulates `docker exec <container> pg_dump`
    by writing predictable binary output to stdout.
    """

    @pytest.fixture()
    def mock_docker(self, tmp_path: Path) -> Path:
        """Create a script that mimics `docker exec <container> pg_dump`.

        The mock ignores the container name and pg_dump args, and just
        writes binary content to stdout (simulating pg_dump custom format).
        """
        script = tmp_path / "mock_docker"
        script.write_text(
            '#!/bin/sh\n'
            '# Mock docker - simulate docker exec <container> pg_dump\n'
            '# Skip "exec" and container name, then act like pg_dump\n'
            'printf "MOCK_DOCKER_PG_DUMP_OUTPUT"\n'
            'exit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    @pytest.fixture()
    def mock_docker_failing(self, tmp_path: Path) -> Path:
        """Mock docker that exits with error."""
        script = tmp_path / "mock_docker_fail"
        script.write_text(
            '#!/bin/sh\n'
            'echo "Error: container not found" >&2\n'
            'exit 1\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    @pytest.fixture()
    def mock_docker_empty(self, tmp_path: Path) -> Path:
        """Mock docker that produces empty output."""
        script = tmp_path / "mock_docker_empty"
        script.write_text(
            '#!/bin/sh\n'
            'exit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    @pytest.fixture()
    def docker_db_config(self, mock_docker: Path) -> DatabaseConfig:
        return DatabaseConfig(
            name="test_docker_pg",
            provider="docker_postgres",
            archive_prefix="databases/test_docker_pg",
            container="immich_postgres",
            dbname="immich",
            user="immich",
            docker_path=str(mock_docker),
        )

    @pytest.mark.unit
    def test_detect_finds_mock(self, docker_db_config, db_logger):
        provider = DockerPostgresProvider(docker_db_config, db_logger)
        assert provider.detect() is True

    @pytest.mark.unit
    def test_detect_missing_binary(self, db_logger):
        cfg = DatabaseConfig(
            name="no_docker",
            provider="docker_postgres",
            archive_prefix="databases/no_docker",
            container="test",
            dbname="test",
            docker_path="/nonexistent/docker",
        )
        provider = DockerPostgresProvider(cfg, db_logger)
        assert provider.detect() is False

    @pytest.mark.integration
    def test_dump_produces_file(self, docker_db_config, db_logger, tmp_path):
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        provider = DockerPostgresProvider(docker_db_config, db_logger)
        files = provider.dump(output_dir)
        assert len(files) == 1
        assert files[0].exists()
        assert files[0].stat().st_size > 0
        assert files[0].read_bytes() == b"MOCK_DOCKER_PG_DUMP_OUTPUT"

    @pytest.mark.integration
    def test_dump_filename_format(self, docker_db_config, db_logger, tmp_path):
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        provider = DockerPostgresProvider(docker_db_config, db_logger)
        files = provider.dump(output_dir)
        name = files[0].name
        assert name.startswith("test_docker_pg_immich_")
        assert name.endswith(".dump")

    @pytest.mark.integration
    def test_dump_fails_on_error(self, mock_docker_failing, db_logger, tmp_path):
        cfg = DatabaseConfig(
            name="fail_test",
            provider="docker_postgres",
            archive_prefix="databases/fail_test",
            container="missing_container",
            dbname="test",
            docker_path=str(mock_docker_failing),
        )
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        provider = DockerPostgresProvider(cfg, db_logger)
        with pytest.raises(RuntimeError, match="docker exec pg_dump failed"):
            provider.dump(output_dir)

    @pytest.mark.integration
    def test_dump_fails_on_empty_output(self, mock_docker_empty, db_logger, tmp_path):
        cfg = DatabaseConfig(
            name="empty_test",
            provider="docker_postgres",
            archive_prefix="databases/empty_test",
            container="test",
            dbname="test",
            docker_path=str(mock_docker_empty),
        )
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        provider = DockerPostgresProvider(cfg, db_logger)
        with pytest.raises(RuntimeError, match="empty output"):
            provider.dump(output_dir)

    @pytest.mark.unit
    def test_missing_container_raises(self, db_logger, tmp_path):
        cfg = DatabaseConfig(
            name="no_container",
            provider="docker_postgres",
            archive_prefix="databases/no_container",
            dbname="test",
            docker_path="docker",
        )
        provider = DockerPostgresProvider(cfg, db_logger)
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        with pytest.raises(RuntimeError, match="requires a 'container'"):
            provider.dump(output_dir)

    @pytest.mark.unit
    def test_missing_dbname_raises(self, db_logger, tmp_path):
        cfg = DatabaseConfig(
            name="no_dbname",
            provider="docker_postgres",
            archive_prefix="databases/no_dbname",
            container="test",
            docker_path="docker",
        )
        provider = DockerPostgresProvider(cfg, db_logger)
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        with pytest.raises(RuntimeError, match="requires a 'dbname'"):
            provider.dump(output_dir)

    @pytest.mark.unit
    def test_get_provider_returns_docker_postgres(self, docker_db_config, db_logger):
        provider = get_provider(docker_db_config, db_logger)
        assert isinstance(provider, DockerPostgresProvider)

    @pytest.mark.unit
    def test_config_parsing(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "docker_test",
            "target_base_dir": str(tmp_path / "dest"),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {
                "immich_db": {
                    "provider": "docker_postgres",
                    "container": "immich_postgres",
                    "dbname": "immich",
                    "user": "immich",
                },
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        config = load_config(cfg_path)

        assert len(config.databases) == 1
        db = config.databases[0]
        assert db.provider == "docker_postgres"
        assert db.container == "immich_postgres"
        assert db.dbname == "immich"
        assert db.user == "immich"
        assert db.docker_path == "docker"

    @pytest.mark.unit
    def test_config_missing_container_raises(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x")
        data = {
            "backup_name": "docker_test",
            "target_base_dir": str(tmp_path / "dest"),
            "keep_last": 0,
            "structure": {"d": {"s": {"source_dir": str(src), "include": "*"}}},
            "databases": {
                "bad_db": {
                    "provider": "docker_postgres",
                    "dbname": "test",
                },
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="container"):
            load_config(cfg_path)

    @pytest.mark.integration
    def test_error_cleans_up_output_file(self, mock_docker_failing, db_logger, tmp_path):
        """Verify that a failed dump does not leave a partial file behind."""
        cfg = DatabaseConfig(
            name="cleanup_test",
            provider="docker_postgres",
            archive_prefix="databases/cleanup_test",
            container="test",
            dbname="test",
            docker_path=str(mock_docker_failing),
        )
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        provider = DockerPostgresProvider(cfg, db_logger)
        with pytest.raises(RuntimeError):
            provider.dump(output_dir)
        # No .dump files should remain
        assert list(output_dir.glob("*.dump")) == []

    @pytest.mark.integration
    def test_extra_args_passed_to_pg_dump(self, tmp_path, db_logger):
        """Verify extra_args appear in the command sent to docker exec."""
        # Mock that echoes the full command to stderr then succeeds
        script = tmp_path / "mock_docker_echo"
        script.write_text(
            '#!/bin/sh\n'
            'echo "$@" >&2\n'
            'printf "DUMP_DATA"\n'
            'exit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        cfg = DatabaseConfig(
            name="args_test",
            provider="docker_postgres",
            archive_prefix="databases/args_test",
            container="mycontainer",
            dbname="mydb",
            user="myuser",
            docker_path=str(script),
            extra_args=["--no-owner", "--clean"],
        )
        output_dir = tmp_path / "staging"
        output_dir.mkdir()
        provider = DockerPostgresProvider(cfg, db_logger)
        files = provider.dump(output_dir)
        assert len(files) == 1
        assert files[0].read_bytes() == b"DUMP_DATA"

    @pytest.mark.integration
    def test_run_dumps_with_docker_provider(
        self, mock_docker, db_logger, tmp_path
    ):
        db_config = DatabaseConfig(
            name="run_test",
            provider="docker_postgres",
            archive_prefix="databases/run_test",
            container="test_container",
            dbname="mydb",
            user="testuser",
            docker_path=str(mock_docker),
        )
        config = BackupConfig(
            backup_name="docker_dump_test",
            target_base_dir=tmp_path / "backups",
            keep_last=0,
            databases=[db_config],
        )
        (tmp_path / "backups").mkdir()

        collected, staging_dir = run_dumps(config, db_logger)
        assert len(collected) == 1
        assert "databases/run_test" in collected[0].archive_path
        assert staging_dir is not None
        # Cleanup
        import shutil
        shutil.rmtree(staging_dir)

    @pytest.mark.integration
    def test_dry_run_docker_postgres(
        self, mock_docker, db_logger, tmp_path
    ):
        """Dry-run with docker_postgres should log container, not host:port."""
        db_config = DatabaseConfig(
            name="dry_docker",
            provider="docker_postgres",
            archive_prefix="databases/dry_docker",
            container="my_container",
            dbname="mydb",
            docker_path=str(mock_docker),
        )
        config = BackupConfig(
            backup_name="dry_test",
            target_base_dir=tmp_path / "backups",
            keep_last=0,
            databases=[db_config],
        )
        collected, staging = run_dumps(config, db_logger, dry_run=True)
        assert len(collected) == 1
        assert staging is None
        assert collected[0].archive_path.startswith("databases/dry_docker/")

    @pytest.mark.integration
    def test_validate_container_not_found(self, tmp_path, db_logger):
        """validate() must raise when docker inspect fails (container missing)."""
        script = tmp_path / "mock_docker_inspect_fail"
        script.write_text(
            '#!/bin/sh\n'
            '# Fail on "inspect", succeed otherwise (for detect)\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "inspect" ]; then\n'
            '    echo "Error: No such container: ghost" >&2\n'
            '    exit 1\n'
            '  fi\n'
            'done\n'
            'printf "DUMP"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        cfg = DatabaseConfig(
            name="ghost_test",
            provider="docker_postgres",
            archive_prefix="databases/ghost_test",
            container="ghost",
            dbname="testdb",
            docker_path=str(script),
        )
        provider = DockerPostgresProvider(cfg, db_logger)
        with pytest.raises(ValueError, match="not found.*ghost"):
            provider.validate()

    @pytest.mark.integration
    def test_validate_container_stopped(self, tmp_path, db_logger):
        """validate() must raise when container exists but is not running."""
        script = tmp_path / "mock_docker_inspect_stopped"
        script.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "inspect" ]; then\n'
            '    echo "false"\n'
            '    exit 0\n'
            '  fi\n'
            'done\n'
            'printf "DUMP"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        cfg = DatabaseConfig(
            name="stopped_test",
            provider="docker_postgres",
            archive_prefix="databases/stopped_test",
            container="stopped_pg",
            dbname="testdb",
            docker_path=str(script),
        )
        provider = DockerPostgresProvider(cfg, db_logger)
        with pytest.raises(ValueError, match="stopped_pg.*not running"):
            provider.validate()

    @pytest.mark.integration
    def test_validate_container_running_passes(self, tmp_path, db_logger):
        """validate() must pass when container is running."""
        script = tmp_path / "mock_docker_inspect_ok"
        script.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "inspect" ]; then\n'
            '    echo "true"\n'
            '    exit 0\n'
            '  fi\n'
            'done\n'
            'printf "DUMP"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        cfg = DatabaseConfig(
            name="ok_test",
            provider="docker_postgres",
            archive_prefix="databases/ok_test",
            container="running_pg",
            dbname="testdb",
            docker_path=str(script),
        )
        provider = DockerPostgresProvider(cfg, db_logger)
        # Must not raise
        provider.validate()

    @pytest.mark.integration
    def test_extra_args_appear_in_command(self, tmp_path, db_logger):
        """Verify extra_args are actually present in the executed command."""
        script = tmp_path / "mock_docker_cmdlog"
        script.write_text(
            '#!/bin/sh\n'
            '# Write full args to a sidecar file, then produce output\n'
            'echo "$@" > "$CMDLOG_PATH"\n'
            'printf "DUMP_DATA"\n'
            'exit 0\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        cmdlog = tmp_path / "cmdlog.txt"
        cfg = DatabaseConfig(
            name="argcheck",
            provider="docker_postgres",
            archive_prefix="databases/argcheck",
            container="mycontainer",
            dbname="mydb",
            user="myuser",
            docker_path=str(script),
            extra_args=["--no-owner", "--clean"],
        )
        old_env = os.environ.get("CMDLOG_PATH")
        os.environ["CMDLOG_PATH"] = str(cmdlog)
        try:
            output_dir = tmp_path / "staging"
            output_dir.mkdir()
            provider = DockerPostgresProvider(cfg, db_logger)
            provider.dump(output_dir)
            recorded_cmd = cmdlog.read_text()
            assert "--no-owner" in recorded_cmd
            assert "--clean" in recorded_cmd
            assert "mycontainer" in recorded_cmd
            assert "--username" in recorded_cmd
            assert "myuser" in recorded_cmd
        finally:
            if old_env is None:
                os.environ.pop("CMDLOG_PATH", None)
            else:
                os.environ["CMDLOG_PATH"] = old_env


# ===================================================================
# Dry-run logging correctness per provider
# ===================================================================
class TestDryRunLogging:
    """Verify dry-run log messages show provider-appropriate details."""

    @pytest.mark.integration
    def test_sqlite_dry_run_shows_db_path(self, db_logger, tmp_path, caplog):
        """SQLite dry-run must log db_path, not host:port."""
        db_config = DatabaseConfig(
            name="sqlite_dry",
            provider="sqlite",
            archive_prefix="databases/sqlite_dry",
            db_path="/opt/app/db.sqlite3",
        )
        config = BackupConfig(
            backup_name="dry_test",
            target_base_dir=tmp_path / "backups",
            keep_last=0,
            subfolders=[SubfolderConfig("d", "s", tmp_path, "*")],
            databases=[db_config],
        )
        with caplog.at_level(logging.INFO, logger="backup.test.db_dump"):
            collected, staging = run_dumps(config, db_logger, dry_run=True)
        assert len(collected) == 1
        assert staging is None
        assert collected[0].archive_path.startswith("databases/sqlite_dry/")
        log_text = caplog.text
        assert "db_path=/opt/app/db.sqlite3" in log_text
        assert "host=" not in log_text

    @pytest.mark.integration
    def test_postgres_dry_run_shows_host_port(
        self, mock_pg_dump, db_logger, tmp_path, caplog
    ):
        """Postgres dry-run must log host:port."""
        db_config = DatabaseConfig(
            name="pg_dry",
            provider="postgres",
            archive_prefix="databases/pg_dry",
            dbname="mydb",
            host="db.local",
            port=5433,
            pg_dump_path=str(mock_pg_dump),
        )
        config = BackupConfig(
            backup_name="dry_test",
            target_base_dir=tmp_path / "backups",
            keep_last=0,
            subfolders=[SubfolderConfig("d", "s", tmp_path, "*")],
            databases=[db_config],
        )
        with caplog.at_level(logging.INFO, logger="backup.test.db_dump"):
            collected, staging = run_dumps(config, db_logger, dry_run=True)
        assert len(collected) == 1
        assert staging is None
        log_text = caplog.text
        assert "host=db.local:5433" in log_text

    @pytest.mark.integration
    def test_docker_dry_run_shows_container(
        self, db_logger, tmp_path, caplog
    ):
        """Docker postgres dry-run must log container name."""
        script = tmp_path / "mock_docker"
        script.write_text('#!/bin/sh\nprintf "DUMP"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        db_config = DatabaseConfig(
            name="docker_dry",
            provider="docker_postgres",
            archive_prefix="databases/docker_dry",
            container="immich_postgres",
            dbname="immich",
            docker_path=str(script),
        )
        config = BackupConfig(
            backup_name="dry_test",
            target_base_dir=tmp_path / "backups",
            keep_last=0,
            subfolders=[SubfolderConfig("d", "s", tmp_path, "*")],
            databases=[db_config],
        )
        with caplog.at_level(logging.INFO, logger="backup.test.db_dump"):
            collected, staging = run_dumps(config, db_logger, dry_run=True)
        assert len(collected) == 1
        assert staging is None
        log_text = caplog.text
        assert "container=immich_postgres" in log_text
        assert "host=" not in log_text
