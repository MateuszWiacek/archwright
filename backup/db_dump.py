"""Database dump providers used by the backup pipeline."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Type

from backup.constants import TIMESTAMP_FORMAT
from backup.models import BackupConfig, CollectedFile, DatabaseConfig

class DatabaseProvider(ABC):
    """Base class for database dump providers."""

    def __init__(
        self,
        config: DatabaseConfig,
        logger: logging.Logger,
        *,
        hook_timeout: int = 300,
        dump_timeout: int = 3600,
    ) -> None:
        self.config = config
        self.logger = logger
        self.hook_timeout = hook_timeout
        self.dump_timeout = dump_timeout

    @abstractmethod
    def detect(self) -> bool: ...

    def validate(self) -> None:
        """Run provider-specific preflight checks."""
        pass

    def pre_backup(self) -> None:
        """Run the optional pre-backup hook."""
        if self.config.stop_command:
            self.logger.info(
                "Stopping service for '%s': %s",
                self.config.name,
                self.config.stop_command,
            )
            _run_shell(self.config.stop_command, self.logger, timeout=self.hook_timeout)

    @abstractmethod
    def dump(self, output_dir: Path) -> List[Path]: ...

    @abstractmethod
    def plan_dump_paths(self, output_dir: Path) -> List[Path]: ...

    def post_backup(self) -> None:
        """Run the optional post-backup hook."""
        if self.config.start_command:
            self.logger.info(
                "Starting service for '%s': %s",
                self.config.name,
                self.config.start_command,
            )
            _run_shell(self.config.start_command, self.logger, timeout=self.hook_timeout)


def _run_shell(
    command: str, logger: logging.Logger, *, timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a shell command, logging output. Raises on failure."""
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout.strip():
        logger.debug("stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.debug("stderr: %s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {command}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _quote_sqlite_path(path: Path) -> str:
    """Quote a path for SQLite dot-commands such as ``.backup``."""
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


class PostgresProvider(DatabaseProvider):
    """Hot PostgreSQL dump via ``pg_dump``."""

    def detect(self) -> bool:
        return shutil.which(self.config.pg_dump_path) is not None

    def validate(self) -> None:
        pg_isready = shutil.which("pg_isready")
        if pg_isready is None:
            self.logger.debug(
                "pg_isready not found, skipping reachability check for '%s'",
                self.config.name,
            )
            return

        cfg = self.config
        cmd = [
            pg_isready,
            "--host", cfg.host,
            "--port", str(cfg.port),
            "--username", cfg.user,
        ]
        if cfg.dbname:
            cmd.extend(["--dbname", cfg.dbname])

        env = os.environ.copy()
        if cfg.password:
            env["PGPASSWORD"] = cfg.password

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                f"pg_isready timed out for '{cfg.name}' "
                f"(host={cfg.host}:{cfg.port})"
            )

        if result.returncode != 0:
            raise ValueError(
                f"PostgreSQL server not reachable for '{cfg.name}' "
                f"(host={cfg.host}:{cfg.port}): {result.stdout.strip()}"
            )

        self.logger.debug(
            "pg_isready OK for '%s' (%s:%d)",
            cfg.name, cfg.host, cfg.port,
        )

    def _build_output_path(self, output_dir: Path) -> Path:
        if not self.config.dbname:
            raise RuntimeError(
                f"Postgres database '{self.config.name}' requires a 'dbname'"
            )

        timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        filename = f"{self.config.name}_{self.config.dbname}_{timestamp}.dump"
        return output_dir / filename

    def plan_dump_paths(self, output_dir: Path) -> List[Path]:
        return [self._build_output_path(output_dir)]

    def dump(self, output_dir: Path) -> List[Path]:
        """Run ``pg_dump`` and write a ``.dump`` file to *output_dir*."""
        cfg = self.config
        output_path = self._build_output_path(output_dir)

        cmd: List[str] = [cfg.pg_dump_path]
        cmd.extend(["--host", cfg.host])
        cmd.extend(["--port", str(cfg.port)])
        cmd.extend(["--username", cfg.user])
        cmd.extend(["--format", "custom"])
        cmd.extend(["--file", str(output_path)])
        cmd.extend(cfg.extra_args)
        cmd.append(cfg.dbname)

        env = os.environ.copy()
        if cfg.password:
            env["PGPASSWORD"] = cfg.password

        self.logger.info(
            "Running pg_dump for '%s' (db=%s, host=%s:%d)",
            cfg.name, cfg.dbname, cfg.host, cfg.port,
        )
        self.logger.debug("Command: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.dump_timeout,
            env=env,
        )

        if result.stderr.strip():
            self.logger.debug("pg_dump stderr: %s", result.stderr.strip())

        if result.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed (exit {result.returncode}) for '{cfg.name}': "
                f"{result.stderr.strip()}"
            )

        if not output_path.exists():
            raise RuntimeError(
                f"pg_dump completed but output file missing: {output_path}"
            )

        size_mb = output_path.stat().st_size / (1024 * 1024)
        self.logger.info(
            "Dump created (%.2f MiB): %s", size_mb, output_path.name
        )
        return [output_path]


class SqliteProvider(DatabaseProvider):
    """Hot SQLite backup via ``sqlite3 .backup``."""

    def detect(self) -> bool:
        return shutil.which(self.config.sqlite3_path) is not None

    def validate(self) -> None:
        db_path = Path(self.config.db_path or "")
        if not db_path.exists():
            raise ValueError(
                f"SQLite database file does not exist for "
                f"'{self.config.name}': {db_path}"
            )
        if not db_path.is_file():
            raise ValueError(
                f"SQLite database path is not a file for "
                f"'{self.config.name}': {db_path}"
            )

    def _build_output_path(self, output_dir: Path) -> Path:
        if not self.config.db_path:
            raise RuntimeError(
                f"SQLite database '{self.config.name}' requires 'db_path'"
            )
        timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        src_stem = Path(self.config.db_path).stem
        filename = f"{self.config.name}_{src_stem}_{timestamp}.sqlite3"
        return output_dir / filename

    def plan_dump_paths(self, output_dir: Path) -> List[Path]:
        return [self._build_output_path(output_dir)]

    def dump(self, output_dir: Path) -> List[Path]:
        """Run ``sqlite3 .backup`` for a hot SQLite copy."""
        cfg = self.config
        if not cfg.db_path:
            raise RuntimeError(
                f"SQLite database '{cfg.name}' requires 'db_path'"
            )

        db_path = Path(cfg.db_path)
        if not db_path.is_file():
            raise RuntimeError(
                f"SQLite database file does not exist: {db_path}"
            )

        output_path = self._build_output_path(output_dir)

        self.logger.info(
            "Running sqlite3 .backup for '%s' (source=%s)",
            cfg.name, db_path,
        )

        # SQLite's .backup command is safe against live writers; plain file
        # copying is not.
        cmd = [
            cfg.sqlite3_path,
            str(db_path),
            f".backup {_quote_sqlite_path(output_path)}",
        ]
        self.logger.debug("Command: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.dump_timeout,
        )

        if result.stderr.strip():
            self.logger.debug("sqlite3 stderr: %s", result.stderr.strip())

        if result.returncode != 0:
            raise RuntimeError(
                f"sqlite3 .backup failed (exit {result.returncode}) "
                f"for '{cfg.name}': {result.stderr.strip()}"
            )

        if not output_path.exists():
            raise RuntimeError(
                f"sqlite3 .backup completed but output file missing: "
                f"{output_path}"
            )

        size_mb = output_path.stat().st_size / (1024 * 1024)
        self.logger.info(
            "SQLite backup created (%.2f MiB): %s", size_mb, output_path.name
        )
        return [output_path]


class DockerPostgresProvider(DatabaseProvider):
    """Run ``pg_dump`` inside a Docker container."""

    def detect(self) -> bool:
        return shutil.which(self.config.docker_path) is not None

    def validate(self) -> None:
        cfg = self.config
        if not cfg.container:
            raise ValueError(
                f"docker_postgres database '{cfg.name}' requires a 'container'"
            )

        cmd = [
            cfg.docker_path, "inspect",
            "--format", "{{.State.Running}}",
            cfg.container,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                f"docker inspect timed out for container '{cfg.container}' "
                f"(database '{cfg.name}')"
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise ValueError(
                f"Container '{cfg.container}' not found for database "
                f"'{cfg.name}': {stderr}"
            )

        running = result.stdout.strip().lower()
        if running != "true":
            raise ValueError(
                f"Container '{cfg.container}' exists but is not running "
                f"for database '{cfg.name}' (state: {running})"
            )

        self.logger.debug(
            "Container '%s' is running for '%s'",
            cfg.container, cfg.name,
        )

    def _build_output_path(self, output_dir: Path) -> Path:
        if not self.config.dbname:
            raise RuntimeError(
                f"docker_postgres database '{self.config.name}' requires a 'dbname'"
            )
        if not self.config.container:
            raise RuntimeError(
                f"docker_postgres database '{self.config.name}' requires a 'container'"
            )
        timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
        filename = f"{self.config.name}_{self.config.dbname}_{timestamp}.dump"
        return output_dir / filename

    def plan_dump_paths(self, output_dir: Path) -> List[Path]:
        return [self._build_output_path(output_dir)]

    def dump(self, output_dir: Path) -> List[Path]:
        """Run ``docker exec ... pg_dump`` and stream stdout to a file."""
        cfg = self.config
        output_path = self._build_output_path(output_dir)

        inner_cmd: List[str] = ["pg_dump"]
        inner_cmd.extend(["--username", cfg.user])
        inner_cmd.extend(["--format", "custom"])
        inner_cmd.extend(cfg.extra_args)
        inner_cmd.append(cfg.dbname)

        cmd: List[str] = [
            cfg.docker_path, "exec", cfg.container,
        ] + inner_cmd

        self.logger.info(
            "Running docker exec pg_dump for '%s' (container=%s, db=%s)",
            cfg.name, cfg.container, cfg.dbname,
        )
        self.logger.debug("Command: %s", " ".join(cmd))

        # Stream stdout straight to disk so large dumps do not sit in RAM.
        try:
            with open(output_path, "wb") as out_f:
                proc = subprocess.Popen(
                    cmd,
                    stdout=out_f,
                    stderr=subprocess.PIPE,
                )
                _, stderr = proc.communicate(timeout=self.dump_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"docker exec pg_dump timed out after {self.dump_timeout}s "
                f"for '{cfg.name}'"
            )

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"docker exec pg_dump failed (exit {proc.returncode}) "
                f"for '{cfg.name}': {stderr_text}"
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"docker exec pg_dump produced empty output for '{cfg.name}'"
            )

        size_mb = output_path.stat().st_size / (1024 * 1024)
        self.logger.info(
            "Dump created (%.2f MiB): %s", size_mb, output_path.name
        )
        return [output_path]


_PROVIDERS: Dict[str, Type[DatabaseProvider]] = {
    "postgres": PostgresProvider,
    "docker_postgres": DockerPostgresProvider,
    "sqlite": SqliteProvider,
}


def get_provider(
    config: DatabaseConfig,
    logger: logging.Logger,
    *,
    hook_timeout: int = 300,
    dump_timeout: int = 3600,
) -> DatabaseProvider:
    """Instantiate the correct provider for *config.provider*."""
    cls = _PROVIDERS.get(config.provider)
    if cls is None:
        raise ValueError(
            f"No provider registered for '{config.provider}'. "
            f"Available: {', '.join(sorted(_PROVIDERS))}"
        )
    return cls(config, logger, hook_timeout=hook_timeout, dump_timeout=dump_timeout)


def validate_dump_prerequisites(
    config: BackupConfig,
    logger: logging.Logger,
) -> None:
    """Validate dump tooling and provider-specific runtime prerequisites."""
    for db_config in config.databases:
        provider = get_provider(
            db_config,
            logger,
            hook_timeout=config.hook_timeout,
            dump_timeout=config.dump_timeout,
        )
        if not provider.detect():
            if db_config.provider == "sqlite":
                tool_name = db_config.sqlite3_path
            elif db_config.provider == "docker_postgres":
                tool_name = db_config.docker_path
            else:
                tool_name = db_config.pg_dump_path
            raise ValueError(
                f"Dump tool not found for '{db_config.name}' "
                f"(provider={db_config.provider}): "
                f"'{tool_name}' is not on PATH"
            )

        provider.validate()


def run_dumps(
    config: BackupConfig,
    logger: logging.Logger,
    *,
    dry_run: bool = False,
    staging_base: Optional[Path] = None,
) -> tuple[List[CollectedFile], Optional[Path]]:
    """Run configured database dumps and return archive-ready files."""
    if not config.databases:
        return [], None

    hook_timeout = config.hook_timeout
    dump_timeout = config.dump_timeout

    if dry_run:
        planned_root = (staging_base or config.target_base_dir) / ".db_staging_dry_run"
        collected: List[CollectedFile] = []

        for db_config in config.databases:
            provider = get_provider(
                db_config,
                logger,
                hook_timeout=hook_timeout,
                dump_timeout=dump_timeout,
            )
            for dump_path in provider.plan_dump_paths(planned_root / db_config.name):
                archive_path = f"{db_config.archive_prefix}/{dump_path.name}"
                if db_config.provider == "docker_postgres":
                    logger.info(
                        "[DRY-RUN] Would dump database '%s' "
                        "(provider=%s, container=%s)",
                        db_config.name,
                        db_config.provider,
                        db_config.container,
                    )
                elif db_config.provider == "sqlite":
                    logger.info(
                        "[DRY-RUN] Would dump database '%s' "
                        "(provider=%s, db_path=%s)",
                        db_config.name,
                        db_config.provider,
                        db_config.db_path,
                    )
                else:
                    logger.info(
                        "[DRY-RUN] Would dump database '%s' "
                        "(provider=%s, host=%s:%d)",
                        db_config.name,
                        db_config.provider,
                        db_config.host,
                        db_config.port,
                    )
                logger.info(
                    "[DRY-RUN]   %s -> %s",
                    dump_path,
                    archive_path,
                )
                collected.append(
                    CollectedFile(
                        source_path=dump_path,
                        archive_path=archive_path,
                    )
                )

        logger.info(
            "[DRY-RUN] Database dumps: %d file(s) would be staged",
            len(collected),
        )
        return collected, None

    base = staging_base or config.target_base_dir
    base.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(dir=str(base), prefix=".db_staging_"))

    collected: List[CollectedFile] = []
    try:
        for db_config in config.databases:
            provider = get_provider(
                db_config,
                logger,
                hook_timeout=hook_timeout,
                dump_timeout=dump_timeout,
            )

            if not provider.detect():
                if db_config.provider == "sqlite":
                    tool_name = db_config.sqlite3_path
                elif db_config.provider == "docker_postgres":
                    tool_name = db_config.docker_path
                else:
                    tool_name = db_config.pg_dump_path
                raise RuntimeError(
                    f"Dump tool not found for '{db_config.name}' "
                    f"(provider={db_config.provider}): "
                    f"'{tool_name}' is not on PATH"
                )

            db_staging = staging_dir / db_config.name
            db_staging.mkdir()

            try:
                provider.pre_backup()
                dump_files = provider.dump(db_staging)
                provider.post_backup()
            except Exception:
                # Try to restart even if the dump failed mid-run.
                if db_config.stop_command and db_config.start_command:
                    try:
                        provider.post_backup()
                    except Exception as restart_err:
                        logger.error(
                            "Failed to restart service after dump error: %s",
                            restart_err,
                        )
                raise

            for dump_path in dump_files:
                archive_path = f"{db_config.archive_prefix}/{dump_path.name}"
                collected.append(
                    CollectedFile(
                        source_path=dump_path.resolve(),
                        archive_path=archive_path,
                    )
                )
        logger.info("Database dumps: %d file(s) staged", len(collected))
        return collected, staging_dir
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
