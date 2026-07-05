"""Integration tests for archwright_web FastAPI routes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

pytestmark = pytest.mark.web


@pytest.fixture(autouse=True)
def reset_job_runner_state():
    import time

    from archwright_web.routers.jobs import runner
    from archwright_web.services.job_runner import JobResult

    runner._result = JobResult()
    yield
    for _ in range(50):
        if not runner.is_running:
            break
        time.sleep(0.01)
    runner._result = JobResult()


def _write_web_config(
    tmp_path: Path,
    config_dir: Path,
    filename: str,
    backup_name: str,
) -> Path:
    src = tmp_path / f"{backup_name}-src"
    src.mkdir()
    (src / "data.txt").write_text(backup_name)

    target = tmp_path / f"{backup_name}-backups"
    target.mkdir()

    config_path = config_dir / filename
    config_path.write_text(yaml.dump({
        "backup_name": backup_name,
        "target_base_dir": str(target),
        "keep_last": 3,
        "structure": {
            backup_name: {
                "files": {
                    "source_dir": str(src),
                    "include": "*",
                }
            }
        },
    }))
    return config_path


@pytest.fixture()
def app_with_config(tmp_path: Path):
    """Create a minimal valid config and return a TestClient."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    (src / "b.yaml").write_text("key: val")

    target = tmp_path / "backups"
    target.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "backup_name": "testapp",
        "target_base_dir": str(target),
        "keep_last": 3,
        "structure": {
            "myapp": {
                "config": {
                    "source_dir": str(src),
                    "include": "*",
                }
            }
        },
    }))

    from archwright_web.app import create_app

    app = create_app(config_path)
    return TestClient(app)


@pytest.fixture()
def app_with_config_dir(tmp_path: Path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_web_config(tmp_path, config_dir, "alpha.yaml", "alpha-app")
    _write_web_config(tmp_path, config_dir, "beta.yaml", "beta-app")

    from archwright_web.app import create_app

    app = create_app(config_dir=config_dir)
    return {
        "client": TestClient(app),
        "alpha_target": tmp_path / "alpha-app-backups",
        "beta_target": tmp_path / "beta-app-backups",
    }


@pytest.fixture()
def app_with_ssh_inventory(tmp_path: Path, monkeypatch):
    from archwright_web.services.executor import JsonCommandResult

    class FakeSSHExecutor:
        def __init__(self, node, timeout=300):
            self.node = node
            self.timeout = timeout

        def list_config_paths(self):
            return JsonCommandResult(
                exit_code=0,
                payload={
                    "ok": True,
                    "configs": ["/etc/archwright/remote.yaml"],
                },
                raw_output="",
            )

        def list_archives(self, config_path: Path):
            return JsonCommandResult(
                exit_code=0,
                payload={
                    "ok": True,
                    "backup_name": "remote-app",
                    "target_base_dir": "/srv/backups",
                    "keep_last": 3,
                    "archives": [{
                        "filename": "remote-app_2026-04-28_12-00-00.zip",
                        "size_mib": 0.25,
                        "modified": "2026-04-28T12:00:00",
                        "log": {"exists": True},
                    }],
                },
                raw_output="",
            )

        def validate(self, config_path: Path):
            return JsonCommandResult(
                exit_code=0,
                payload={"ok": True, "backup_name": "remote-app"},
                raw_output="",
            )

        def backup_dry_run(self, config_path: Path):
            return JsonCommandResult(
                exit_code=0,
                payload={
                    "ok": True,
                    "dry_run": True,
                    "backup_name": "remote-app",
                    "total_entries": 2,
                },
                raw_output="",
            )

        def backup(self, config_path: Path, *, on_line=None):
            lines = [
                "Remote backup started",
                "Archive created: /srv/backups/remote-app.zip",
            ]
            if on_line is not None:
                for line in lines:
                    on_line(line)
            return JsonCommandResult(
                exit_code=0,
                payload={"ok": True, "phase": "backup"},
                raw_output="\n".join(lines) + "\n",
            )

        def restore_dry_run(
            self,
            config_path: Path,
            archive_path: Path,
            *,
            selected_prefixes=None,
            overwrite=False,
        ):
            plan = [
                {
                    "archive_path": "app/files/config.yaml",
                    "target_path": "/opt/app/config.yaml",
                    "target_exists": True,
                },
                {
                    "archive_path": "data/files/data.txt",
                    "target_path": "/srv/data/data.txt",
                    "target_exists": False,
                },
            ]
            if selected_prefixes is not None:
                plan = [
                    entry for entry in plan
                    if "/".join(entry["archive_path"].split("/")[:2])
                    in selected_prefixes
                ]
            conflicts = [
                entry for entry in plan if entry["target_exists"]
            ]
            return JsonCommandResult(
                exit_code=0,
                payload={
                    "ok": True,
                    "dry_run": True,
                    "archive": str(archive_path),
                    "overwrite": overwrite,
                    "plan_count": len(plan),
                    "conflict_count": len(conflicts),
                    "plan": plan,
                    "conflicts": conflicts,
                },
                raw_output="",
            )

    monkeypatch.setattr(
        "archwright_web.services.config_registry.SSHExecutor",
        FakeSSHExecutor,
    )
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

    from archwright_web.app import create_app

    return TestClient(create_app(inventory_path=inventory_path))


class TestDashboard:
    def test_get_dashboard(self, app_with_config):
        resp = app_with_config.get("/")
        assert resp.status_code == 200
        assert "testapp" in resp.text
        assert "ARCHWRIGHT" in resp.text.upper()

    def test_dashboard_shows_stats(self, app_with_config):
        resp = app_with_config.get("/")
        assert "Archives" in resp.text or "ARCHIVES" in resp.text.upper()
        assert "Sources" in resp.text or "SOURCES" in resp.text.upper()


class TestMultiConfigDashboard:
    def test_dashboard_lists_config_jobs(self, app_with_config_dir):
        resp = app_with_config_dir["client"].get("/")
        assert resp.status_code == 200
        assert "alpha" in resp.text
        assert "beta" in resp.text
        assert "alpha-app" in resp.text

    def test_dashboard_selects_job_from_query(self, app_with_config_dir):
        resp = app_with_config_dir["client"].get("/?job=beta")
        assert resp.status_code == 200
        assert "beta-app" in resp.text
        assert "alpha-app" not in resp.text

    def test_archives_use_selected_job(self, app_with_config_dir):
        beta_target = app_with_config_dir["beta_target"]
        (beta_target / "beta-app_2024-01-01_00-00-00.zip").write_bytes(
            b"PK\x03\x04" + b"\x00" * 100
        )

        resp = app_with_config_dir["client"].get("/archives/?job=beta")
        assert resp.status_code == 200
        assert "beta-app_2024-01-01_00-00-00.zip" in resp.text

        resp = app_with_config_dir["client"].get("/archives/?job=alpha")
        assert resp.status_code == 200
        assert "beta-app_2024-01-01_00-00-00.zip" not in resp.text

    def test_unknown_job_does_not_trigger_validate(
        self, app_with_config_dir, monkeypatch
    ):
        from archwright_web.routers import jobs

        called = []

        def fake_run_validate(config_path: Path) -> bool:
            called.append(config_path)
            return True

        monkeypatch.setattr(jobs.runner, "run_validate", fake_run_validate)

        resp = app_with_config_dir["client"].post("/jobs/validate?job=missing")

        assert resp.status_code == 200
        assert "Unknown config job: missing" in resp.text
        assert called == []


class TestSSHInventoryRoutes:
    def test_dashboard_lists_remote_archives(self, app_with_ssh_inventory):
        resp = app_with_ssh_inventory.get("/")

        assert resp.status_code == 200
        assert "remote:remote" in resp.text
        assert "remote-app" in resp.text
        assert "remote-app_2026-04-28_12-00-00.zip" in resp.text
        assert "BACKUP NOW" in resp.text

    def test_archives_lists_remote_archives_without_local_actions(
        self, app_with_ssh_inventory
    ):
        resp = app_with_ssh_inventory.get("/archives/")

        assert resp.status_code == 200
        assert "remote-app_2026-04-28_12-00-00.zip" in resp.text
        assert "remote" in resp.text
        assert "/download" not in resp.text

    def test_remote_validate_returns_immediate_job_status(
        self, app_with_ssh_inventory
    ):
        resp = app_with_ssh_inventory.post("/jobs/validate")

        assert resp.status_code == 200
        assert "remote validate completed" in resp.text
        assert "remote-app" in resp.text

    def test_remote_dry_run_returns_immediate_job_status(
        self, app_with_ssh_inventory
    ):
        resp = app_with_ssh_inventory.post("/jobs/backup/dry-run")

        assert resp.status_code == 200
        assert "remote dry-run backup completed" in resp.text
        assert "total_entries" in resp.text

    def test_remote_live_backup_starts_background_job(
        self, app_with_ssh_inventory
    ):
        import time

        resp = app_with_ssh_inventory.post("/jobs/backup")

        assert resp.status_code == 200
        assert "remote backup" in resp.text
        for _ in range(50):
            status = app_with_ssh_inventory.get("/jobs/status")
            if "remote backup completed" in status.text:
                break
            time.sleep(0.01)
        assert "remote backup completed" in status.text

    def test_remote_restore_start_lists_archives(
        self, app_with_ssh_inventory
    ):
        resp = app_with_ssh_inventory.get("/restore/")

        assert resp.status_code == 200
        assert "remote-app_2026-04-28_12-00-00.zip" in resp.text

    def test_remote_restore_plan_renders_with_stream_notice(
        self, app_with_ssh_inventory
    ):
        resp = app_with_ssh_inventory.post(
            "/restore/plan",
            data={"archive": "remote-app_2026-04-28_12-00-00.zip"},
        )

        assert resp.status_code == 200
        assert "archwright will run on the remote node over SSH" in resp.text
        assert "app/files/config.yaml" in resp.text
        assert "data/files/data.txt" in resp.text
        assert "EXISTS" in resp.text

    def test_remote_restore_filter_uses_ssh_dry_run(
        self, app_with_ssh_inventory
    ):
        resp = app_with_ssh_inventory.post(
            "/restore/plan/filter",
            data={
                "archive": "remote-app_2026-04-28_12-00-00.zip",
                "prefixes": "data/files",
            },
        )

        assert resp.status_code == 200
        assert "data/files/data.txt" in resp.text
        assert "app/files/config.yaml" not in resp.text


class TestConfigEditor:
    def test_get_config_page(self, app_with_config):
        resp = app_with_config.get("/config/")
        assert resp.status_code == 200
        assert "testapp" in resp.text
        assert "YAML" in resp.text.upper()

    def test_preview_yaml(self, app_with_config):
        resp = app_with_config.post("/config/preview-yaml", data={
            "backup_name": "edited",
            "target_base_dir": "/tmp/out",
            "keep_last": "5",
            "log_level": "INFO",
            "hook_timeout": "300",
            "dump_timeout": "3600",
            "sf_0_folder": "app",
            "sf_0_subfolder": "data",
            "sf_0_source_dir": "/tmp/src",
            "sf_0_include": "*.json",
        })
        assert resp.status_code == 200
        assert "edited" in resp.text
        assert "*.json" in resp.text

    def test_preview_yaml_bad_numeric(self, app_with_config):
        resp = app_with_config.post("/config/preview-yaml", data={
            "backup_name": "test",
            "target_base_dir": "/tmp",
            "keep_last": "not_a_number",
            "log_level": "INFO",
            "hook_timeout": "bad",
            "dump_timeout": "also_bad",
        })
        assert resp.status_code == 200  # graceful fallback, not 500

    def test_export_yaml_get(self, app_with_config):
        resp = app_with_config.get("/config/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-yaml")
        assert "testapp" in resp.text

    def test_export_yaml_post(self, app_with_config):
        resp = app_with_config.post("/config/export", data={
            "backup_name": "exported",
            "target_base_dir": "/tmp/out",
            "keep_last": "2",
            "log_level": "INFO",
            "hook_timeout": "300",
            "dump_timeout": "3600",
            "sf_0_folder": "app",
            "sf_0_subfolder": "cfg",
            "sf_0_source_dir": "/data",
            "sf_0_include": "*.yaml",
        })
        assert resp.status_code == 200
        assert "exported" in resp.text

    def test_export_preserves_db_fields(self, app_with_config):
        resp = app_with_config.post("/config/export", data={
            "backup_name": "full",
            "target_base_dir": "/tmp",
            "keep_last": "1",
            "log_level": "INFO",
            "hook_timeout": "300",
            "dump_timeout": "3600",
            "sf_0_folder": "app",
            "sf_0_subfolder": "f",
            "sf_0_source_dir": "/tmp",
            "sf_0_include": "*",
            "db_0_name": "mydb",
            "db_0_provider": "postgres",
            "db_0_dbname": "appdb",
            "db_0_host": "db.local",
            "db_0_port": "5433",
            "db_0_user": "admin",
            "db_0_password": "s3cret",
            "db_0_pg_dump_path": "/opt/bin/pg_dump",
            "db_0_stop_command": "systemctl stop pg",
            "db_0_start_command": "systemctl start pg",
            "db_0_extra_args": "--no-owner, --if-exists",
        })
        assert resp.status_code == 200
        parsed = yaml.safe_load(resp.text)
        db = parsed["databases"]["mydb"]
        assert db["password"] == "s3cret"
        assert db["pg_dump_path"] == "/opt/bin/pg_dump"
        assert db["stop_command"] == "systemctl stop pg"
        assert db["start_command"] == "systemctl start pg"
        assert db["extra_args"] == ["--no-owner", "--if-exists"]


class TestGlobPreview:
    def test_preview_returns_matches(self, app_with_config, tmp_path):
        src = tmp_path / "src"  # already created by fixture
        resp = app_with_config.post("/preview/glob", data={
            "source_dir": str(src),
            "include": "*.txt",
            "exclude": "",
        })
        assert resp.status_code == 200
        assert "1 file" in resp.text

    def test_preview_bad_dir(self, app_with_config):
        resp = app_with_config.post("/preview/glob", data={
            "source_dir": "/nonexistent/path",
            "include": "*",
        })
        assert resp.status_code == 200
        assert "does not exist" in resp.text

    def test_preview_empty_input(self, app_with_config):
        resp = app_with_config.post("/preview/glob", data={
            "source_dir": "",
            "include": "",
        })
        assert resp.status_code == 200


class TestArchives:
    def test_list_empty(self, app_with_config):
        resp = app_with_config.get("/archives/")
        assert resp.status_code == 200
        assert "No archives" in resp.text

    def test_list_shows_archive_listing_error(self, tmp_path: Path):
        from archwright_web.app import create_app

        src = tmp_path / "src"
        src.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "backup_name": "testapp",
            "target_base_dir": str(tmp_path / "missing-backups"),
            "keep_last": 3,
            "structure": {
                "myapp": {
                    "config": {
                        "source_dir": str(src),
                        "include": "*",
                    }
                }
            },
        }))

        resp = TestClient(create_app(config_path)).get("/archives/")

        assert resp.status_code == 200
        assert "Target directory does not exist" in resp.text

    def test_download_missing(self, app_with_config):
        resp = app_with_config.get(
            "/archives/testapp_2099-01-01_00-00-00.zip/download",
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_contents_bad_zip_returns_400(self, app_with_config, tmp_path):
        target = tmp_path / "backups"
        (target / "testapp_2024-01-01_00-00-00.zip").write_text("not a zip")

        resp = app_with_config.get(
            "/archives/testapp_2024-01-01_00-00-00.zip/contents"
        )

        assert resp.status_code == 400
        assert "Invalid ZIP archive" in resp.text

    def test_binary_entry_download_header_is_quoted(self, app_with_config, tmp_path):
        import zipfile

        target = tmp_path / "backups"
        zp = target / "testapp_2024-01-01_00-00-00.zip"
        entry_name = 'dir/report 1;".bin'
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(entry_name, b"\xff\xfe")

        resp = app_with_config.get(
            "/archives/testapp_2024-01-01_00-00-00.zip/entry",
            params={"path": entry_name},
        )

        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith(
            'attachment; filename="report 1__.bin"'
        )
        assert "filename*=UTF-8''report%201%3B%22.bin" in resp.headers[
            "content-disposition"
        ]


class TestJobs:
    def test_status_idle(self, app_with_config):
        resp = app_with_config.get("/jobs/status")
        assert resp.status_code == 200
        assert "idle" in resp.text.lower() or "Idle" in resp.text

    def test_validate_starts(self, app_with_config):
        resp = app_with_config.post("/jobs/validate")
        assert resp.status_code == 200

    def test_backup_dry_run_starts(self, app_with_config):
        resp = app_with_config.post("/jobs/backup/dry-run")
        assert resp.status_code == 200


class TestRestore:
    def test_restore_start_page(self, app_with_config):
        resp = app_with_config.get("/restore/")
        assert resp.status_code == 200
        assert "Restore" in resp.text

    def test_restore_plan_no_archive(self, app_with_config):
        resp = app_with_config.post("/restore/plan", data={"archive": ""})
        assert resp.status_code == 200

    def test_restore_plan_missing_archive(self, app_with_config):
        resp = app_with_config.post(
            "/restore/plan", data={"archive": "nonexistent.zip"}
        )
        assert resp.status_code == 200
        assert "not found" in resp.text.lower() or "Invalid" in resp.text

    def test_restore_plan_path_traversal(self, app_with_config):
        resp = app_with_config.post(
            "/restore/plan", data={"archive": "../../etc/passwd"}
        )
        assert resp.status_code == 200
        assert "Invalid" in resp.text

    def test_restore_plan_with_real_archive(self, app_with_config, tmp_path):
        import zipfile
        target = tmp_path / "backups"
        zp = target / "testapp_2024-01-01_00-00-00.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("myapp/config/a.txt", "hello")

        resp = app_with_config.post(
            "/restore/plan",
            data={"archive": "testapp_2024-01-01_00-00-00.zip"},
        )
        assert resp.status_code == 200
        assert "file(s) to restore" in resp.text
        assert 'id="restore-confirm-form"' in resp.text
        assert 'type="hidden" name="prefixes"' not in resp.text

    def test_restore_filter_empty_selection_means_empty_plan(
        self, app_with_config, tmp_path
    ):
        import zipfile
        target = tmp_path / "backups"
        zp = target / "testapp_2024-01-01_00-00-00.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("myapp/config/a.txt", "hello")

        resp = app_with_config.post(
            "/restore/plan/filter",
            data={"archive": "testapp_2024-01-01_00-00-00.zip"},
        )

        assert resp.status_code == 200
        assert "0 file(s) to restore" in resp.text

    def test_restore_confirm_requires_restore_keyword(self, app_with_config, tmp_path):
        import zipfile
        target = tmp_path / "backups"
        zp = target / "testapp_2024-01-01_00-00-00.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("myapp/config/a.txt", "hello")

        resp = app_with_config.post("/restore/execute", data={
            "archive": "testapp_2024-01-01_00-00-00.zip",
            "confirmation": "wrong",
        })
        assert resp.status_code == 200
        assert "Type RESTORE to confirm" in resp.text

    def test_archive_path_traversal_rejected(self, app_with_config):
        resp = app_with_config.post(
            "/restore/plan/filter",
            data={"archive": "../../../etc/shadow"},
        )
        assert resp.status_code == 400


class TestArchiveSecurity:
    def test_download_path_traversal(self, app_with_config):
        resp = app_with_config.get(
            "/archives/..%2F..%2Fetc%2Fpasswd/download",
            follow_redirects=False,
        )
        assert resp.status_code in (400, 404)

    def test_contents_path_traversal(self, app_with_config):
        resp = app_with_config.get("/archives/..%2Fsecret/contents")
        assert resp.status_code in (400, 404)


class TestLogSecurity:
    def test_log_path_traversal(self, app_with_config):
        resp = app_with_config.get("/logs/..%2F..%2Ftmp%2Fsecret.log")
        assert resp.status_code in (400, 404)

    def test_log_raw_path_traversal(self, app_with_config):
        resp = app_with_config.get("/logs/..%2F..%2Ftmp%2Fsecret.log/raw")
        assert resp.status_code in (400, 404)
