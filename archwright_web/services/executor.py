"""Execution adapters for web control paths."""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from archwright_web.services.inventory import InventoryNode

# Default timeout for short-lived JSON/text commands (validate, list,
# dry-run, config discovery). Live backups use BACKUP_TIMEOUT instead.
DEFAULT_TIMEOUT = 300

# Real backups can run for hours on TB-scale repositories. Cap at 24h
# so a runaway process eventually frees the job lock.
BACKUP_TIMEOUT = 24 * 60 * 60

LineCallback = Callable[[str], None]


@dataclass(frozen=True)
class JsonCommandResult:
    exit_code: int
    payload: Dict[str, Any]
    raw_output: str
    raw_error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.payload.get("ok") is True


class LocalExecutor:
    """Run archwright commands in the current process."""

    def list_archives(self, config_path: Path) -> JsonCommandResult:
        from backup.orchestrator import run_list

        return _capture_json(lambda: run_list(config_path, json_output=True))

    def validate(self, config_path: Path) -> JsonCommandResult:
        from backup.orchestrator import run_validate

        return _capture_json(lambda: run_validate(config_path, json_output=True))

    def backup_dry_run(self, config_path: Path) -> JsonCommandResult:
        from backup.orchestrator import run

        return _capture_json(lambda: run(
            config_path,
            dry_run=True,
            json_output=True,
        ))

    def restore_dry_run(
        self,
        config_path: Path,
        archive_path: Path,
        *,
        selected_prefixes: Optional[List[str]] = None,
        overwrite: bool = False,
    ) -> JsonCommandResult:
        from backup.orchestrator import run_restore

        return _capture_json(lambda: run_restore(
            config_path,
            archive_path,
            selected_prefixes=selected_prefixes,
            overwrite=overwrite,
            dry_run=True,
            json_output=True,
        ))


class SSHExecutor:
    """Run archwright JSON commands on a remote host over SSH."""

    def __init__(
        self,
        node: InventoryNode,
        *,
        ssh_path: str = "ssh",
        timeout: int = DEFAULT_TIMEOUT,
        backup_timeout: int = BACKUP_TIMEOUT,
    ) -> None:
        if not node.is_ssh:
            raise ValueError(f"Node '{node.id}' is not an SSH executor.")
        if not node.host or not node.user:
            raise ValueError(f"SSH node '{node.id}' requires host and user.")
        self.node = node
        self.ssh_path = ssh_path
        self.timeout = timeout
        self.backup_timeout = backup_timeout

    def list_archives(self, config_path: Path) -> JsonCommandResult:
        return self._run_json([
            "list",
            "--config", str(config_path),
            "--json",
        ])

    def validate(self, config_path: Path) -> JsonCommandResult:
        return self._run_json([
            "validate",
            "--config", str(config_path),
            "--json",
        ])

    def backup_dry_run(self, config_path: Path) -> JsonCommandResult:
        return self._run_json([
            "backup",
            "--config", str(config_path),
            "--dry-run",
            "--json",
        ])

    def backup(
        self,
        config_path: Path,
        *,
        on_line: Optional[LineCallback] = None,
    ) -> JsonCommandResult:
        ssh_command = self._build_ssh_command([
            "backup",
            "--config", str(config_path),
        ])
        return self._run_streaming(
            ssh_command,
            phase="backup",
            on_line=on_line,
            timeout=self.backup_timeout,
        )

    def restore_dry_run(
        self,
        config_path: Path,
        archive_path: Path,
        *,
        selected_prefixes: Optional[List[str]] = None,
        overwrite: bool = False,
    ) -> JsonCommandResult:
        args = [
            "restore",
            "--config", str(config_path),
            "--archive", str(archive_path),
            "--dry-run",
            "--json",
        ]
        if selected_prefixes:
            args.append("--only")
            args.extend(selected_prefixes)
        if overwrite:
            args.append("--overwrite")
        return self._run_json(args)

    def restore(
        self,
        config_path: Path,
        archive_path: Path,
        *,
        selected_prefixes: Optional[List[str]] = None,
        overwrite: bool = False,
        on_line: Optional[LineCallback] = None,
    ) -> JsonCommandResult:
        """Run a live restore on the remote node and stream stdout lines.

        Uses the same streaming machinery as ``backup`` so the UI can show
        progress while the remote ``archwright restore`` works through the
        archive. Cap matches ``backup_timeout`` because large restores can
        run for hours.
        """
        args = [
            "restore",
            "--config", str(config_path),
            "--archive", str(archive_path),
        ]
        if selected_prefixes:
            args.append("--only")
            args.extend(selected_prefixes)
        if overwrite:
            args.append("--overwrite")
        ssh_command = self._build_ssh_command(args)
        return self._run_streaming(
            ssh_command,
            phase="restore",
            on_line=on_line,
            timeout=self.backup_timeout,
        )

    def list_config_paths(self) -> JsonCommandResult:
        config_dir = shlex.quote(self.node.config_dir)
        remote_command = (
            f"find {config_dir} -maxdepth 1 -type f "
            r"\( -name '*.yaml' -o -name '*.yml' \) -print | sort"
        )
        return self._run_text(remote_command, phase="config_discovery")

    def _run_json(self, archwright_args: List[str]) -> JsonCommandResult:
        ssh_command = self._build_ssh_command(archwright_args)
        return self._run_command(ssh_command)

    def _run_text(self, remote_command: str, *, phase: str) -> JsonCommandResult:
        result = self._run_command(
            self._build_remote_command(remote_command),
            parse_json=False,
            phase=phase,
        )
        if not result.ok:
            return result
        configs = [
            line.strip()
            for line in result.raw_output.splitlines()
            if line.strip()
        ]
        return JsonCommandResult(
            exit_code=0,
            payload={"ok": True, "phase": phase, "configs": configs},
            raw_output=result.raw_output,
            raw_error=result.raw_error,
        )

    def _run_command(
        self,
        ssh_command: List[str],
        *,
        parse_json: bool = True,
        phase: str = "command",
    ) -> JsonCommandResult:
        try:
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return JsonCommandResult(
                exit_code=1,
                payload={
                    "ok": False,
                    "phase": "ssh_timeout",
                    "error": (
                        f"SSH command timed out after {self.timeout}s "
                        f"for node '{self.node.id}'"
                    ),
                },
                raw_output=exc.stdout or "",
                raw_error=exc.stderr or "",
            )

        if not parse_json:
            payload = {"ok": result.returncode == 0, "phase": phase}
            if result.returncode != 0:
                payload.update({
                    "error": result.stderr.strip()
                    or f"Command failed with exit code {result.returncode}",
                })
            return JsonCommandResult(
                exit_code=result.returncode,
                payload=payload,
                raw_output=result.stdout,
                raw_error=result.stderr,
            )

        return _parse_json_result(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _run_streaming(
        self,
        ssh_command: List[str],
        *,
        phase: str,
        on_line: Optional[LineCallback],
        timeout: int,
    ) -> JsonCommandResult:
        """Run an SSH command, streaming stdout lines to on_line as they arrive.

        Used for live operations (currently `backup`) where the user needs to
        see progress in the UI rather than waiting for the whole command to
        finish before any logs appear.
        """
        try:
            proc = subprocess.Popen(
                ssh_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            return JsonCommandResult(
                exit_code=1,
                payload={
                    "ok": False,
                    "phase": "ssh_spawn",
                    "error": f"Failed to start SSH command: {exc}",
                },
                raw_output="",
                raw_error=str(exc),
            )

        stdout_lines: List[str] = []
        stderr_chunks: List[str] = []

        def drain_stderr() -> None:
            assert proc.stderr is not None
            stderr_chunks.append(proc.stderr.read())

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        timed_out = threading.Event()

        def kill_after_timeout() -> None:
            if proc.poll() is None:
                timed_out.set()
                try:
                    proc.kill()
                except OSError:
                    pass

        timeout_timer = threading.Timer(timeout, kill_after_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                stdout_lines.append(line)
                if on_line and line.strip():
                    try:
                        on_line(line)
                    except Exception:  # nosec B110 - never let a UI-side callback failure abort a long-running remote backup; the line is also captured in stdout_lines for later return
                        pass
            exit_code = proc.wait()
        finally:
            timeout_timer.cancel()

        stderr_thread.join(timeout=5)
        raw_error = "".join(stderr_chunks)
        raw_output = "\n".join(stdout_lines)
        if stdout_lines:
            raw_output += "\n"

        if timed_out.is_set():
            return JsonCommandResult(
                exit_code=1,
                payload={
                    "ok": False,
                    "phase": "ssh_timeout",
                    "error": (
                        f"SSH command timed out after {timeout}s "
                        f"for node '{self.node.id}'"
                    ),
                },
                raw_output=raw_output,
                raw_error=raw_error,
            )

        payload: Dict[str, Any] = {"ok": exit_code == 0, "phase": phase}
        if exit_code != 0:
            payload["error"] = (
                raw_error.strip()
                or f"Command failed with exit code {exit_code}"
            )
        return JsonCommandResult(
            exit_code=exit_code,
            payload=payload,
            raw_output=raw_output,
            raw_error=raw_error,
        )

    def _build_ssh_command(self, archwright_args: List[str]) -> List[str]:
        remote_args = shlex.split(self.node.command) + archwright_args
        remote_command = " ".join(shlex.quote(arg) for arg in remote_args)
        return self._build_remote_command(remote_command)

    def _build_remote_command(self, remote_command: str) -> List[str]:
        connect_timeout = min(self.timeout, 30)
        return [
            self.ssh_path,
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-p", str(self.node.port),
            f"{self.node.user}@{self.node.host}",
            remote_command,
        ]


def _capture_json(command: Callable[[], int]) -> JsonCommandResult:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = command()

    return _parse_json_result(
        exit_code=exit_code,
        stdout=stdout.getvalue(),
        stderr="",
    )


_MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024  # 16 MiB cap on remote JSON


def _parse_json_result(
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> JsonCommandResult:
    if len(stdout) > _MAX_JSON_RESPONSE_BYTES:
        # Defensive cap. A misbehaving or compromised remote node could
        # return a multi-GB blob; refuse to parse rather than OOM the host.
        return JsonCommandResult(
            exit_code=1,
            payload={
                "ok": False,
                "phase": "json_decode",
                "error": (
                    f"Response exceeds {_MAX_JSON_RESPONSE_BYTES} bytes "
                    f"(got {len(stdout)} bytes)"
                ),
            },
            raw_output=stdout[:1024],
            raw_error=stderr,
        )
    try:
        decoded = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as exc:
        payload = {
            "ok": False,
            "phase": "json_decode",
            "error": str(exc),
        }
    else:
        if isinstance(decoded, dict):
            payload = decoded
        else:
            payload = {
                "ok": False,
                "phase": "json_decode",
                "error": "Expected JSON object from command output",
            }

    if not payload.get("ok") and exit_code != 0:
        error = payload.get("error") or stderr.strip()
        phase = payload.get("phase") or "command"
        payload = {
            "ok": False,
            "phase": phase,
            "error": error or f"Command failed with exit code {exit_code}",
        }

    return JsonCommandResult(
        exit_code=exit_code,
        payload=payload,
        raw_output=stdout,
        raw_error=stderr,
    )


local_executor = LocalExecutor()
