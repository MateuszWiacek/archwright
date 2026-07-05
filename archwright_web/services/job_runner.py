"""Job runner with file-based locking and background execution."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from archwright_web.services.executor import JsonCommandResult


class JobStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class JobResult:
    status: JobStatus = JobStatus.IDLE
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    action: str = ""
    exit_code: Optional[int] = None
    log_lines: List[str] = field(default_factory=list)
    error: Optional[str] = None


class _LogCapture(logging.Handler):
    """Logging handler that captures lines for the UI."""

    def __init__(self, result: JobResult) -> None:
        super().__init__()
        self.result = result
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.result.log_lines.append(self.format(record))


class JobRunner:
    """Singleton-style runner: one job at a time, lock-protected."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result = JobResult()
        self._thread: Optional[threading.Thread] = None

    @property
    def current(self) -> JobResult:
        return self._result

    @property
    def is_running(self) -> bool:
        return self._result.status == JobStatus.RUNNING

    def run_backup(
        self, config_path: Path, *, dry_run: bool = False
    ) -> bool:
        """Start a backup in a background thread. Returns False if already running."""
        if not self._lock.acquire(blocking=False):
            return False

        action = "dry-run backup" if dry_run else "backup"
        self._result = JobResult(
            status=JobStatus.RUNNING,
            started_at=datetime.now(),
            action=action,
        )

        self._thread = threading.Thread(
            target=self._exec_backup,
            args=(config_path, dry_run),
            daemon=True,
        )
        self._thread.start()
        return True

    def run_validate(self, config_path: Path) -> bool:
        """Start validation in a background thread. Returns False if already running."""
        if not self._lock.acquire(blocking=False):
            return False

        self._result = JobResult(
            status=JobStatus.RUNNING,
            started_at=datetime.now(),
            action="validate",
        )

        self._thread = threading.Thread(
            target=self._exec_validate,
            args=(config_path,),
            daemon=True,
        )
        self._thread.start()
        return True

    def run_remote_backup(
        self,
        command: Callable[[Callable[[str], None]], "JsonCommandResult"],
    ) -> bool:
        """Start a remote backup in a background thread.

        ``command`` is a streaming callable: it receives a log-line callback
        that should be invoked once per stdout line as the remote process
        produces it, so the UI can show progress instead of waiting for the
        whole backup (which can take hours) to finish.
        """
        return self._run_remote(command, action="remote backup")

    def run_remote_restore(
        self,
        command: Callable[[Callable[[str], None]], "JsonCommandResult"],
    ) -> bool:
        """Start a remote restore in a background thread.

        Same streaming contract as ``run_remote_backup``: ``command`` is
        a callable that takes a log-line callback. Restores can run for
        hours on large archives, so we stream rather than wait.
        """
        return self._run_remote(command, action="remote restore")

    def _run_remote(
        self,
        command: Callable[[Callable[[str], None]], "JsonCommandResult"],
        *,
        action: str,
    ) -> bool:
        if not self._lock.acquire(blocking=False):
            return False

        self._result = JobResult(
            status=JobStatus.RUNNING,
            started_at=datetime.now(),
            action=action,
        )

        self._thread = threading.Thread(
            target=self._exec_remote_command,
            args=(command,),
            daemon=True,
        )
        self._thread.start()
        return True

    def _append_log_line(self, line: str) -> None:
        self._result.log_lines.append(line)

    def _exec_remote_command(
        self,
        command: Callable[[Callable[[str], None]], "JsonCommandResult"],
    ) -> None:
        try:
            result = command(self._append_log_line)
            self._result.exit_code = result.exit_code
            # If the streaming callback already populated log_lines, keep them;
            # otherwise fall back to whatever the result carries.
            if not self._result.log_lines:
                self._result.log_lines = _remote_result_lines(result)
            self._result.error = _remote_result_error(result)
            self._result.status = (
                JobStatus.SUCCESS if result.ok else JobStatus.FAILED
            )
        except Exception as exc:
            self._result.status = JobStatus.FAILED
            self._result.error = str(exc)
            self._result.exit_code = 1
        finally:
            self._result.finished_at = datetime.now()
            self._lock.release()

    def _exec_backup(self, config_path: Path, dry_run: bool) -> None:
        try:
            from backup.orchestrator import run

            handler = _LogCapture(self._result)
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)

            try:
                exit_code = run(config_path, dry_run=dry_run)
            finally:
                root_logger.removeHandler(handler)

            self._result.exit_code = exit_code
            self._result.status = (
                JobStatus.SUCCESS if exit_code == 0 else JobStatus.FAILED
            )
        except Exception as exc:
            self._result.status = JobStatus.FAILED
            self._result.error = str(exc)
            self._result.exit_code = 1
        finally:
            self._result.finished_at = datetime.now()
            self._lock.release()

    def run_restore(
        self,
        config_path: Path,
        *,
        archive: str,
        selected_prefixes: Optional[List[str]] = None,
        overwrite: bool = False,
    ) -> bool:
        """Start a restore in a background thread. Returns False if already running."""
        if not self._lock.acquire(blocking=False):
            return False

        self._result = JobResult(
            status=JobStatus.RUNNING,
            started_at=datetime.now(),
            action="restore",
        )

        self._thread = threading.Thread(
            target=self._exec_restore,
            args=(config_path, archive, selected_prefixes, overwrite),
            daemon=True,
        )
        self._thread.start()
        return True

    def _exec_restore(
        self,
        config_path: Path,
        archive: str,
        selected_prefixes: Optional[List[str]],
        overwrite: bool,
    ) -> None:
        try:
            from backup.config import load_config
            from backup.restore import execute_restore, plan_restore
            from archwright_web.services.safe_paths import safe_child_path

            handler = _LogCapture(self._result)
            logger = logging.getLogger("archwright.restore")
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)

            try:
                config = load_config(config_path)
                zip_path = safe_child_path(
                    config.target_base_dir,
                    archive,
                    label="archive name",
                    required_suffix=".zip",
                )
                plan = plan_restore(
                    zip_path, config, logger,
                    selected_prefixes=selected_prefixes,
                )
                execute_restore(
                    zip_path, plan, logger, overwrite=overwrite,
                )
            finally:
                logger.removeHandler(handler)

            self._result.exit_code = 0
            self._result.status = JobStatus.SUCCESS
        except Exception as exc:
            self._result.status = JobStatus.FAILED
            self._result.error = str(exc)
            self._result.exit_code = 1
        finally:
            self._result.finished_at = datetime.now()
            self._lock.release()

    def _exec_validate(self, config_path: Path) -> None:
        try:
            from backup.orchestrator import run_validate

            handler = _LogCapture(self._result)
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)

            try:
                exit_code = run_validate(config_path)
            finally:
                root_logger.removeHandler(handler)

            self._result.exit_code = exit_code
            self._result.status = (
                JobStatus.SUCCESS if exit_code == 0 else JobStatus.FAILED
            )
        except Exception as exc:
            self._result.status = JobStatus.FAILED
            self._result.error = str(exc)
            self._result.exit_code = 1
        finally:
            self._result.finished_at = datetime.now()
            self._lock.release()


def _remote_result_lines(result) -> List[str]:
    lines: List[str] = []
    for output in (result.raw_output, result.raw_error):
        lines.extend(line for line in output.splitlines() if line.strip())
    if not lines and result.payload:
        lines = json.dumps(result.payload, indent=2).splitlines()
    return lines


def _remote_result_error(result) -> Optional[str]:
    if result.ok:
        return None
    if result.payload:
        error = result.payload.get("error")
        if error:
            return str(error)
    if result.raw_error.strip():
        return result.raw_error.strip()
    if result.raw_output.strip():
        return result.raw_output.strip().splitlines()[-1]
    return f"Remote command failed with exit code {result.exit_code}"


# Module-level singleton shared across the app.
runner = JobRunner()
