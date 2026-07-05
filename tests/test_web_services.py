"""Unit tests for archwright_web service layer."""

from __future__ import annotations

import zipfile
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest
import yaml

from backup.models import BackupConfig, DatabaseConfig, SubfolderConfig

pytestmark = pytest.mark.web


# ---------------------------------------------------------------------------
# config_service
# ---------------------------------------------------------------------------

class TestConfigService:
    def test_config_to_dict_round_trip(self, tmp_path: Path):
        from archwright_web.services.config_service import config_to_dict, export_yaml

        config = BackupConfig(
            backup_name="myapp",
            target_base_dir=tmp_path,
            keep_last=3,
            subfolders=[
                SubfolderConfig(
                    folder_name="app",
                    subfolder_name="config",
                    source_dir=tmp_path / "src",
                    include="*.yaml",
                    exclude="*.tmp",
                    pre_command="docker stop app",
                    post_command="docker start app",
                ),
            ],
            databases=[
                DatabaseConfig(
                    name="maindb",
                    provider="postgres",
                    archive_prefix="databases/maindb",
                    dbname="mydb",
                    host="db.local",
                    port=5433,
                    user="admin",
                    password="secret",
                    extra_args=["--no-owner"],
                    stop_command="systemctl stop pg",
                    start_command="systemctl start pg",
                ),
            ],
            log_level="DEBUG",
            hook_timeout=120,
            dump_timeout=1800,
        )

        d = config_to_dict(config)

        assert d["backup_name"] == "myapp"
        assert d["keep_last"] == 3
        assert d["log_level"] == "DEBUG"
        assert d["hook_timeout"] == 120
        assert d["dump_timeout"] == 1800

        sf = d["structure"]["app"]["config"]
        assert sf["include"] == "*.yaml"
        assert sf["exclude"] == "*.tmp"
        assert sf["pre_command"] == "docker stop app"
        assert sf["post_command"] == "docker start app"

        db = d["databases"]["maindb"]
        assert db["provider"] == "postgres"
        assert db["dbname"] == "mydb"
        assert db["host"] == "db.local"
        assert db["port"] == 5433
        assert db["user"] == "admin"
        assert db["password"] == "secret"
        assert db["extra_args"] == ["--no-owner"]
        assert db["stop_command"] == "systemctl stop pg"
        assert db["start_command"] == "systemctl start pg"

        yaml_text = export_yaml(config)
        assert "myapp" in yaml_text
        assert "secret" in yaml_text

    def test_config_to_dict_defaults_omitted(self, tmp_path: Path):
        from archwright_web.services.config_service import config_to_dict

        config = BackupConfig(
            backup_name="simple",
            target_base_dir=tmp_path,
            keep_last=0,
            subfolders=[
                SubfolderConfig(
                    folder_name="f", subfolder_name="s",
                    source_dir=tmp_path, include="*",
                ),
            ],
        )

        d = config_to_dict(config)
        assert "log_level" not in d
        assert "hook_timeout" not in d
        assert "dump_timeout" not in d
        assert "databases" not in d

    def test_docker_postgres_fields(self, tmp_path: Path):
        from archwright_web.services.config_service import config_to_dict

        config = BackupConfig(
            backup_name="dock",
            target_base_dir=tmp_path,
            keep_last=1,
            subfolders=[
                SubfolderConfig(
                    folder_name="f", subfolder_name="s",
                    source_dir=tmp_path, include="*",
                ),
            ],
            databases=[
                DatabaseConfig(
                    name="dockdb",
                    provider="docker_postgres",
                    archive_prefix="databases/dockdb",
                    container="pg_container",
                    dbname="appdb",
                    user="app",
                    docker_path="/usr/local/bin/docker",
                ),
            ],
        )

        db = config_to_dict(config)["databases"]["dockdb"]
        assert db["container"] == "pg_container"
        assert db["docker_path"] == "/usr/local/bin/docker"
        assert db["user"] == "app"

    def test_sqlite_fields(self, tmp_path: Path):
        from archwright_web.services.config_service import config_to_dict

        config = BackupConfig(
            backup_name="lite",
            target_base_dir=tmp_path,
            keep_last=1,
            subfolders=[
                SubfolderConfig(
                    folder_name="f", subfolder_name="s",
                    source_dir=tmp_path, include="*",
                ),
            ],
            databases=[
                DatabaseConfig(
                    name="litedb",
                    provider="sqlite",
                    archive_prefix="databases/litedb",
                    db_path="/data/app.sqlite3",
                    sqlite3_path="/usr/bin/sqlite3",
                ),
            ],
        )

        db = config_to_dict(config)["databases"]["litedb"]
        assert db["db_path"] == "/data/app.sqlite3"
        assert db["sqlite3_path"] == "/usr/bin/sqlite3"


class TestConfigRegistry:
    def test_config_dir_discovers_yaml_jobs(self, tmp_path: Path):
        from archwright_web.services.config_registry import create_source, list_jobs

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "target"
        target.mkdir()

        for name in ("alpha.yaml", "beta.yml"):
            (config_dir / name).write_text(yaml.dump({
                "backup_name": name.split(".")[0],
                "target_base_dir": str(target),
                "keep_last": 1,
                "structure": {
                    "app": {
                        "files": {
                            "source_dir": str(src),
                            "include": "*",
                        }
                    }
                },
            }))

        jobs = list_jobs(create_source(config_dir=config_dir))

        assert [job.id for job in jobs] == ["alpha", "beta"]
        assert all(job.config is not None for job in jobs)

    def test_config_dir_keeps_invalid_jobs_visible(self, tmp_path: Path):
        from archwright_web.services.config_registry import create_source, list_jobs

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        (config_dir / "broken.yaml").write_text("backup_name: broken\n")

        jobs = list_jobs(create_source(config_dir=config_dir))

        assert len(jobs) == 1
        assert jobs[0].id == "broken"
        assert jobs[0].config is None
        assert jobs[0].error

    def test_unknown_job_selection_has_no_selected_config(self, tmp_path: Path):
        from archwright_web.services.config_registry import (
            create_source,
            select_job,
        )

        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "target"
        target.mkdir()
        (config_dir / "alpha.yaml").write_text(yaml.dump({
            "backup_name": "alpha",
            "target_base_dir": str(target),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        selection = select_job(create_source(config_dir=config_dir), "missing")

        assert selection.selected is None
        assert selection.error == "Unknown config job: missing"

    def test_inventory_source_discovers_local_node_configs(self, tmp_path: Path):
        from archwright_web.services.config_registry import create_source, list_jobs

        config_dir = tmp_path / "local-node-configs"
        config_dir.mkdir()
        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "target"
        target.mkdir()
        (config_dir / "alpha.yaml").write_text(yaml.dump({
            "backup_name": "alpha",
            "target_base_dir": str(target),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))
        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "local-node": {
                    "executor": "local",
                    "config_dir": str(config_dir),
                },
            },
        }))

        jobs = list_jobs(create_source(inventory_path=inventory_path))

        assert len(jobs) == 1
        assert jobs[0].id == "local-node:alpha"
        assert jobs[0].node_id == "local-node"
        assert jobs[0].executor == "local"
        assert jobs[0].config is not None

    def test_inventory_source_discovers_ssh_node_configs(
        self, tmp_path: Path, monkeypatch
    ):
        from archwright_web.services.config_registry import create_source, list_jobs
        from archwright_web.services.executor import JsonCommandResult

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "remote": {
                    "executor": "ssh",
                    "host": "app-node.example",
                    "user": "archwright",
                    "config_dir": "/etc/archwright",
                },
            },
        }))

        class FakeSSHExecutor:
            def __init__(self, node, timeout=300):
                self.node = node
                self.timeout = timeout

            def list_config_paths(self):
                return JsonCommandResult(
                    exit_code=0,
                    payload={
                        "ok": True,
                        "configs": [
                            "/etc/archwright/alpha.yaml",
                            "/etc/archwright/beta.yml",
                        ],
                    },
                    raw_output="",
                )

        monkeypatch.setattr(
            "archwright_web.services.config_registry.SSHExecutor",
            FakeSSHExecutor,
        )

        jobs = list_jobs(create_source(inventory_path=inventory_path))

        assert [job.id for job in jobs] == ["remote:alpha", "remote:beta"]
        assert jobs[0].executor == "ssh"
        assert jobs[0].node_id == "remote"
        assert jobs[0].config is None
        assert jobs[0].error is None

    def test_inventory_source_marks_ssh_discovery_error(
        self, tmp_path: Path, monkeypatch
    ):
        from archwright_web.services.config_registry import create_source, list_jobs
        from archwright_web.services.executor import JsonCommandResult

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "remote": {
                    "executor": "ssh",
                    "host": "app-node.example",
                    "user": "archwright",
                    "config_dir": "/etc/archwright",
                },
            },
        }))

        class FakeSSHExecutor:
            def __init__(self, node, timeout=300):
                self.node = node
                self.timeout = timeout

            def list_config_paths(self):
                return JsonCommandResult(
                    exit_code=255,
                    payload={
                        "ok": False,
                        "error": "Host key verification failed",
                    },
                    raw_output="",
                    raw_error="",
                )

        monkeypatch.setattr(
            "archwright_web.services.config_registry.SSHExecutor",
            FakeSSHExecutor,
        )

        jobs = list_jobs(create_source(inventory_path=inventory_path))

        assert len(jobs) == 1
        assert jobs[0].id == "remote"
        assert jobs[0].executor == "ssh"
        assert jobs[0].config is None
        assert "Cannot discover YAML configs" in str(jobs[0].error)
        assert "Host key verification failed" in str(jobs[0].error)


class TestInventoryService:
    def test_load_inventory_with_local_and_ssh_nodes(self, tmp_path: Path):
        from archwright_web.services.inventory import load_inventory

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "local-node": {
                    "executor": "local",
                    "config_dir": "/etc/archwright",
                },
                "remote-node": {
                    "executor": "ssh",
                    "host": "app-node.example",
                    "user": "archwright",
                    "port": 2222,
                    "config_dir": "/opt/archwright/configs",
                    "command": "/opt/archwright/bin/archwright",
                },
            },
        }))

        inventory = load_inventory(inventory_path)

        assert inventory.path == inventory_path.resolve()
        assert [node.id for node in inventory.nodes] == ["local-node", "remote-node"]
        assert inventory.get_node("local-node").is_local is True
        assert inventory.get_node("local-node").local_config_dir == Path("/etc/archwright")
        assert inventory.get_node("remote-node").is_ssh is True
        assert inventory.get_node("remote-node").host == "app-node.example"
        assert inventory.get_node("remote-node").user == "archwright"
        assert inventory.get_node("remote-node").port == 2222
        assert inventory.get_node("remote-node").command == "/opt/archwright/bin/archwright"

    def test_inventory_defaults_ssh_command_and_port(self, tmp_path: Path):
        from archwright_web.services.inventory import load_inventory

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "worker": {
                    "executor": "ssh",
                    "host": "worker.local",
                    "user": "archwright",
                    "config_dir": "/etc/archwright",
                },
            },
        }))

        node = load_inventory(inventory_path).get_node("worker")

        assert node.port == 22
        assert node.command == "archwright"

    def test_inventory_rejects_missing_nodes(self, tmp_path: Path):
        from archwright_web.services.inventory import load_inventory

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text("nodes: {}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="at least one node"):
            load_inventory(inventory_path)

    def test_inventory_rejects_unknown_executor(self, tmp_path: Path):
        from archwright_web.services.inventory import load_inventory

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "bad": {
                    "executor": "agent",
                    "config_dir": "/etc/archwright",
                },
            },
        }))

        with pytest.raises(ValueError, match="unsupported executor"):
            load_inventory(inventory_path)

    def test_inventory_rejects_ssh_without_host(self, tmp_path: Path):
        from archwright_web.services.inventory import load_inventory

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "remote": {
                    "executor": "ssh",
                    "user": "archwright",
                    "config_dir": "/etc/archwright",
                },
            },
        }))

        with pytest.raises(ValueError, match="requires host"):
            load_inventory(inventory_path)

    def test_inventory_rejects_bad_node_id(self, tmp_path: Path):
        from archwright_web.services.inventory import load_inventory

        inventory_path = tmp_path / "inventory.yaml"
        inventory_path.write_text(yaml.dump({
            "nodes": {
                "bad/node": {
                    "executor": "local",
                    "config_dir": "/etc/archwright",
                },
            },
        }))

        with pytest.raises(ValueError, match="node ids"):
            load_inventory(inventory_path)


# ---------------------------------------------------------------------------
# archive_service
# ---------------------------------------------------------------------------

class TestArchiveService:
    def test_list_archives(self, tmp_path: Path):
        from archwright_web.services.archive_service import list_archives

        (tmp_path / "myapp_2024-01-01_00-00-00.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        (tmp_path / "myapp_2024-01-01_00-00-00.log").write_text("ok")
        (tmp_path / "myapp_2024-01-02_00-00-00.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 200)

        result = list_archives(tmp_path, "myapp")
        assert len(result) == 2
        assert result[0].filename == "myapp_2024-01-01_00-00-00.zip"
        assert result[0].has_log is True
        assert result[1].has_log is False

    def test_list_archives_empty_dir(self, tmp_path: Path):
        from archwright_web.services.archive_service import list_archives

        assert list_archives(tmp_path, "nonexistent") == []

    def test_list_archives_missing_dir(self):
        from archwright_web.services.archive_service import list_archives

        assert list_archives(Path("/does/not/exist"), "x") == []

    def test_list_archives_from_config_surfaces_executor_error(
        self, tmp_path: Path
    ):
        from archwright_web.services.archive_service import (
            ArchiveListError,
            list_archives_from_config,
        )

        src = tmp_path / "src"
        src.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "backup_name": "missing_target",
            "target_base_dir": str(tmp_path / "missing"),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        with pytest.raises(ArchiveListError, match="Target directory"):
            list_archives_from_config(config_path)

    def test_list_zip_contents(self, tmp_path: Path):
        from archwright_web.services.archive_service import list_zip_contents

        zp = tmp_path / "test.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("file1.txt", "hello")
            zf.writestr("dir/file2.txt", "world")

        entries = list_zip_contents(zp)
        paths = [e.path for e in entries]
        assert "file1.txt" in paths
        assert "dir/file2.txt" in paths
        assert all(not e.is_dir for e in entries if e.path in paths)

    def test_stream_zip_entry(self, tmp_path: Path):
        from archwright_web.services.archive_service import stream_zip_entry

        zp = tmp_path / "test.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("hello.txt", "hello world")

        gen = stream_zip_entry(zp, "hello.txt")
        assert gen is not None
        data = b"".join(gen)
        assert data == b"hello world"

    def test_stream_zip_entry_missing(self, tmp_path: Path):
        from archwright_web.services.archive_service import stream_zip_entry

        zp = tmp_path / "test.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("a.txt", "a")

        gen = stream_zip_entry(zp, "nonexistent.txt")
        assert gen is not None
        assert b"".join(gen) == b""

    def test_stream_zip_entry_missing_file(self):
        from archwright_web.services.archive_service import stream_zip_entry

        assert stream_zip_entry(Path("/no/such/file.zip"), "x") is None

    def test_get_zip_entry_size(self, tmp_path: Path):
        from archwright_web.services.archive_service import get_zip_entry_size

        zp = tmp_path / "test.zip"
        content = b"x" * 1234
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("data.bin", content)

        assert get_zip_entry_size(zp, "data.bin") == 1234
        assert get_zip_entry_size(zp, "missing.bin") is None
        assert get_zip_entry_size(Path("/no/file"), "x") is None


class TestLocalExecutor:
    def test_validate_returns_json_payload(self, tmp_path: Path):
        from archwright_web.services.executor import LocalExecutor

        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "backups"
        target.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "backup_name": "local",
            "target_base_dir": str(target),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        result = LocalExecutor().validate(config_path)

        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["backup_name"] == "local"
        assert result.payload["checks"][-1] == {
            "name": "dump_prerequisites",
            "ok": True,
        }

    def test_archive_listing_can_use_config_json(self, tmp_path: Path):
        from archwright_web.services.archive_service import list_archives_from_config

        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "backups"
        target.mkdir()
        archive = target / "local_2026-04-28_12-00-00.zip"
        archive.write_bytes(b"PK\x03\x04")
        archive.with_suffix(".log").write_text("ok")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "backup_name": "local",
            "target_base_dir": str(target),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        archives = list_archives_from_config(config_path)

        assert len(archives) == 1
        assert archives[0].filename == archive.name
        assert archives[0].has_log is True

    def test_backup_dry_run_returns_json_payload(self, tmp_path: Path):
        from archwright_web.services.executor import LocalExecutor

        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        target = tmp_path / "backups"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "backup_name": "local",
            "target_base_dir": str(target),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        result = LocalExecutor().backup_dry_run(config_path)

        assert result.ok is True
        assert result.payload["dry_run"] is True
        assert result.payload["total_entries"] == 1
        assert target.exists() is False

    def test_restore_dry_run_returns_json_payload(self, tmp_path: Path):
        from archwright_web.services.executor import LocalExecutor

        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "backups"
        target.mkdir()
        archive = target / "local_2026-04-28_12-00-00.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("app/files/file.txt", "hello")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "backup_name": "local",
            "target_base_dir": str(target),
            "keep_last": 1,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        result = LocalExecutor().restore_dry_run(config_path, archive)

        assert result.ok is True
        assert result.payload["dry_run"] is True
        assert result.payload["plan_count"] == 1
        assert result.payload["plan"][0]["archive_path"] == "app/files/file.txt"


class TestSSHExecutor:
    def test_validate_builds_ssh_command_and_parses_json(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append({
                "cmd": cmd,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            })
            return CompletedProcess(
                cmd,
                0,
                stdout='{"ok": true, "command": "validate"}',
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="app-node.example",
            user="operator",
            port=2222,
            config_dir="/etc/archwright",
            command="/opt/archwright/bin/archwright",
        )

        result = SSHExecutor(node, timeout=42).validate(
            Path("/etc/archwright/job.yaml")
        )

        assert result.ok is True
        assert result.payload["command"] == "validate"
        assert calls == [{
            "cmd": [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=30",
                "-p", "2222",
                "operator@app-node.example",
                (
                    "/opt/archwright/bin/archwright validate "
                    "--config /etc/archwright/job.yaml --json"
                ),
            ],
            "capture_output": True,
            "text": True,
            "timeout": 42,
        }]

    def test_restore_dry_run_quotes_remote_arguments(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
            command="uv run archwright",
        )

        result = SSHExecutor(node).restore_dry_run(
            Path("/etc/archwright/job one.yaml"),
            Path("/backups/archive one.zip"),
            selected_prefixes=["app/config", "logs/main logs"],
            overwrite=True,
        )

        assert result.ok is True
        assert calls[0] == [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=30",
            "-p", "22",
            "archwright@backup.example",
            (
                "uv run archwright restore --config "
                "'/etc/archwright/job one.yaml' --archive "
                "'/backups/archive one.zip' --dry-run --json --only "
                "app/config 'logs/main logs' --overwrite"
            ),
        ]

    def test_backup_runs_remote_text_command(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        calls = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                calls.append(cmd)
                import io as _io
                self.stdout = _io.StringIO("Starting backup\nArchive created\n")
                self.stderr = _io.StringIO("")
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr("subprocess.Popen", FakePopen)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
            command="archwright",
        )

        seen_lines = []
        result = SSHExecutor(node).backup(
            Path("/etc/archwright/job.yaml"),
            on_line=seen_lines.append,
        )

        assert result.ok is True
        assert result.payload == {"ok": True, "phase": "backup"}
        assert result.raw_output == "Starting backup\nArchive created\n"
        assert seen_lines == ["Starting backup", "Archive created"]
        assert calls[0][-1] == "archwright backup --config /etc/archwright/job.yaml"

    def test_backup_uses_long_timeout_for_live_run(self, monkeypatch):
        from archwright_web.services.executor import (
            BACKUP_TIMEOUT,
            JsonCommandResult,
            SSHExecutor,
        )
        from archwright_web.services.inventory import InventoryNode

        seen_timeout = {}

        def fake_run_streaming(self, ssh_command, *, phase, on_line, timeout):
            seen_timeout["value"] = timeout
            return JsonCommandResult(
                exit_code=0,
                payload={"ok": True, "phase": phase},
                raw_output="done\n",
            )

        monkeypatch.setattr(SSHExecutor, "_run_streaming", fake_run_streaming)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
            command="archwright",
        )

        SSHExecutor(node).backup(Path("/etc/archwright/job.yaml"))

        assert seen_timeout["value"] == BACKUP_TIMEOUT
        assert seen_timeout["value"] >= 60 * 60

    def test_restore_streams_and_quotes_remote_arguments(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        calls = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                calls.append(cmd)
                import io as _io
                self.stdout = _io.StringIO("Restoring data/files/data.txt\nRestore complete\n")
                self.stderr = _io.StringIO("")
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr("subprocess.Popen", FakePopen)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
            command="archwright",
        )

        seen_lines = []
        result = SSHExecutor(node).restore(
            Path("/etc/archwright/job one.yaml"),
            Path("/backups/archive one.zip"),
            selected_prefixes=["app/config"],
            overwrite=True,
            on_line=seen_lines.append,
        )

        assert result.ok is True
        assert seen_lines == ["Restoring data/files/data.txt", "Restore complete"]
        assert calls[0][-1] == (
            "archwright restore --config '/etc/archwright/job one.yaml' "
            "--archive '/backups/archive one.zip' --only app/config --overwrite"
        )

    def test_restore_uses_long_timeout_for_live_run(self, monkeypatch):
        from archwright_web.services.executor import (
            BACKUP_TIMEOUT,
            JsonCommandResult,
            SSHExecutor,
        )
        from archwright_web.services.inventory import InventoryNode

        seen_timeout = {}

        def fake_run_streaming(self, ssh_command, *, phase, on_line, timeout):
            seen_timeout["value"] = timeout
            return JsonCommandResult(
                exit_code=0,
                payload={"ok": True, "phase": phase},
                raw_output="done\n",
            )

        monkeypatch.setattr(SSHExecutor, "_run_streaming", fake_run_streaming)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
            command="archwright",
        )

        SSHExecutor(node).restore(
            Path("/etc/archwright/job.yaml"),
            Path("/backups/archive.zip"),
        )

        assert seen_timeout["value"] == BACKUP_TIMEOUT

    def test_streaming_timeout_kills_process_while_stdout_is_open(self):
        import sys
        import time

        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
            command="archwright",
        )
        executor = SSHExecutor(node)
        started_at = time.monotonic()

        result = executor._run_streaming(
            [
                sys.executable,
                "-c",
                (
                    "import time; "
                    "print('started', flush=True); "
                    "time.sleep(10)"
                ),
            ],
            phase="backup",
            on_line=None,
            timeout=0.2,
        )

        assert time.monotonic() - started_at < 3
        assert result.ok is False
        assert result.payload["phase"] == "ssh_timeout"
        assert result.raw_output == "started\n"

    def test_list_config_paths_discovers_remote_yaml_files(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return CompletedProcess(
                cmd,
                0,
                stdout="/etc/archwright/a.yaml\n/etc/archwright/b.yml\n",
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
        )

        result = SSHExecutor(node).list_config_paths()

        assert result.ok is True
        assert result.payload["configs"] == [
            "/etc/archwright/a.yaml",
            "/etc/archwright/b.yml",
        ]
        assert calls[0][-1].startswith("find /etc/archwright")

    def test_non_json_ssh_failure_returns_structured_error(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        def fake_run(cmd, capture_output, text, timeout):
            return CompletedProcess(
                cmd,
                255,
                stdout="",
                stderr="ssh: connect to host failed",
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
        )

        result = SSHExecutor(node).list_archives(Path("/etc/archwright/job.yaml"))

        assert result.ok is False
        assert result.exit_code == 255
        assert result.payload["phase"] == "command"
        assert result.payload["error"] == "ssh: connect to host failed"
        assert result.raw_error == "ssh: connect to host failed"

    def test_ssh_timeout_returns_structured_error(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        def fake_run(cmd, capture_output, text, timeout):
            raise TimeoutExpired(cmd=cmd, timeout=timeout)

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
        )

        result = SSHExecutor(node, timeout=5).backup_dry_run(
            Path("/etc/archwright/job.yaml")
        )

        assert result.ok is False
        assert result.exit_code == 1
        assert result.payload["phase"] == "ssh_timeout"
        assert "remote" in result.payload["error"]

    def test_connect_timeout_never_exceeds_subprocess_timeout(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        calls = []

        def fake_run(cmd, capture_output, text, timeout):
            calls.append(cmd)
            return CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
        )

        SSHExecutor(node, timeout=5).validate(Path("/etc/archwright/job.yaml"))

        assert calls[0][3:5] == ["-o", "ConnectTimeout=5"]

    def test_failed_json_payload_preserves_remote_error(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        def fake_run(cmd, capture_output, text, timeout):
            return CompletedProcess(
                cmd,
                1,
                stdout='{"ok": false, "phase": "config", "error": "bad config"}',
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
        )

        result = SSHExecutor(node).validate(Path("/etc/archwright/job.yaml"))

        assert result.ok is False
        assert result.payload["phase"] == "config"
        assert result.payload["error"] == "bad config"

    def test_non_object_json_returns_structured_error(self, monkeypatch):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        def fake_run(cmd, capture_output, text, timeout):
            return CompletedProcess(cmd, 1, stdout="null", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        node = InventoryNode(
            id="remote",
            executor="ssh",
            host="backup.example",
            user="archwright",
            config_dir="/etc/archwright",
        )

        result = SSHExecutor(node).validate(Path("/etc/archwright/job.yaml"))

        assert result.ok is False
        assert result.payload["phase"] == "json_decode"
        assert result.payload["error"] == "Expected JSON object from command output"

    def test_ssh_executor_rejects_local_node(self):
        from archwright_web.services.executor import SSHExecutor
        from archwright_web.services.inventory import InventoryNode

        node = InventoryNode(
            id="local-node",
            executor="local",
            config_dir="/etc/archwright",
        )

        with pytest.raises(ValueError, match="not an SSH executor"):
            SSHExecutor(node)


# ---------------------------------------------------------------------------
# collector_service
# ---------------------------------------------------------------------------

class TestCollectorService:
    def test_preview_glob_matches(self, tmp_path: Path):
        from archwright_web.services.collector_service import preview_glob

        (tmp_path / "a.yaml").write_text("a")
        (tmp_path / "b.yaml").write_text("b")
        (tmp_path / "c.txt").write_text("c")

        result = preview_glob(str(tmp_path), "*.yaml")
        assert result.total_count == 2
        assert not result.truncated
        assert result.error is None

    def test_preview_glob_with_exclude(self, tmp_path: Path):
        from archwright_web.services.collector_service import preview_glob

        (tmp_path / "a.yaml").write_text("a")
        (tmp_path / "b.yaml").write_text("b")

        result = preview_glob(str(tmp_path), "*.yaml", exclude="b.yaml")
        assert result.total_count == 1

    def test_preview_glob_bad_dir(self):
        from archwright_web.services.collector_service import preview_glob

        result = preview_glob("/nonexistent/path", "*")
        assert result.error is not None
        assert result.total_count == 0


# ---------------------------------------------------------------------------
# job_runner
# ---------------------------------------------------------------------------

class TestJobRunner:
    def test_initial_state(self):
        from archwright_web.services.job_runner import JobRunner, JobStatus

        r = JobRunner()
        assert r.current.status == JobStatus.IDLE
        assert not r.is_running

    def test_lock_prevents_concurrent_runs(self, tmp_path: Path):
        from archwright_web.services.job_runner import JobRunner

        r = JobRunner()
        # Manually acquire the lock to simulate a running job
        r._lock.acquire()
        try:
            assert r.run_backup(tmp_path / "fake.yaml") is False
            assert r.run_validate(tmp_path / "fake.yaml") is False
            assert r.run_restore(
                tmp_path / "fake.yaml", archive="x.zip"
            ) is False
            assert r.run_remote_backup(lambda: None) is False
        finally:
            r._lock.release()

    def test_validate_runs_to_completion(self, tmp_path: Path):
        import time
        from archwright_web.services.job_runner import JobRunner, JobStatus

        config_yaml = tmp_path / "config.yaml"
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")

        target = tmp_path / "backups"
        target.mkdir()

        config_yaml.write_text(yaml.dump({
            "backup_name": "test",
            "target_base_dir": str(target),
            "keep_last": 0,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        r = JobRunner()
        assert r.run_validate(config_yaml) is True

        # Wait for completion
        for _ in range(50):
            if not r.is_running:
                break
            time.sleep(0.1)

        assert r.current.status == JobStatus.SUCCESS
        assert r.current.exit_code == 0
        assert r.current.finished_at is not None
        assert len(r.current.log_lines) > 0

    def test_remote_backup_runs_to_completion(self):
        import time
        from types import SimpleNamespace

        from archwright_web.services.job_runner import JobRunner, JobStatus

        r = JobRunner()

        def fake_command(on_line):
            on_line("Remote backup starting")
            on_line("Remote backup complete")
            return SimpleNamespace(
                exit_code=0,
                payload={"ok": True, "phase": "backup"},
                raw_output="Remote backup starting\nRemote backup complete\n",
                raw_error="",
                ok=True,
            )

        assert r.run_remote_backup(fake_command) is True

        for _ in range(50):
            if not r.is_running:
                break
            time.sleep(0.1)

        assert r.current.status == JobStatus.SUCCESS
        assert r.current.exit_code == 0
        assert r.current.error is None
        assert "Remote backup starting" in r.current.log_lines
        assert "Remote backup complete" in r.current.log_lines

    def test_restore_runner_rejects_archive_traversal(self, tmp_path: Path):
        import time
        from archwright_web.services.job_runner import JobRunner, JobStatus

        config_yaml = tmp_path / "config.yaml"
        src = tmp_path / "src"
        src.mkdir()
        target = tmp_path / "backups"
        target.mkdir()

        config_yaml.write_text(yaml.dump({
            "backup_name": "test",
            "target_base_dir": str(target),
            "keep_last": 0,
            "structure": {
                "app": {
                    "files": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        r = JobRunner()
        assert r.run_restore(config_yaml, archive="../evil.zip") is True

        for _ in range(50):
            if not r.is_running:
                break
            time.sleep(0.1)

        assert r.current.status == JobStatus.FAILED
        assert r.current.exit_code == 1
        assert "Invalid archive name" in str(r.current.error)
