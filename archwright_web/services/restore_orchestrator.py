"""Restore orchestration: hides local vs remote dispatch from routers.

The wizard router calls into this service for archive listing, plan
building, and execution. The router only handles HTTP/template concerns;
all backup-domain logic lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from archwright_web.services import archive_service, config_registry
from archwright_web.services.inventory import EXECUTOR_LOCAL
from archwright_web.services.job_runner import runner
from archwright_web.services.safe_paths import safe_child_path


class RestoreInputError(ValueError):
    """Caller-side error: invalid config reference or unsafe archive name.

    Routes should map this to HTTP 400. It indicates a bad request, not a
    domain-level planning failure that the user can act on through the UI.
    """


@dataclass
class ArchivesView:
    """Data shown on step 1 (choose archive)."""
    archives: List[Any] = field(default_factory=list)
    config: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class PlanView:
    """Data shown on steps 2-4 (preview / filter / confirm)."""
    plan: List[Any] = field(default_factory=list)
    conflicts: List[Any] = field(default_factory=list)
    prefixes: List[str] = field(default_factory=list)
    config: Optional[Any] = None
    is_remote: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_archive_path(base_dir: Path, archive: str) -> Path:
    try:
        return safe_child_path(
            base_dir,
            archive,
            label="archive name",
            required_suffix=".zip",
        )
    except ValueError as exc:
        raise RestoreInputError(str(exc)) from exc


def _require_config(selection: Any):
    try:
        return config_registry.require_config(selection)
    except ValueError as exc:
        raise RestoreInputError(str(exc)) from exc


def _list_local_archives(
    config_path: Path,
) -> Tuple[List[archive_service.ArchiveInfo], Optional[str]]:
    try:
        return archive_service.list_archives_from_config(config_path), None
    except archive_service.ArchiveListError as exc:
        return [], str(exc)


def _prefixes_from_plan(plan: List[Any]) -> List[str]:
    prefixes: set[str] = set()
    for entry in plan:
        archive_path = entry.get("archive_path") if isinstance(entry, dict) else ""
        parts = str(archive_path).split("/")
        if len(parts) >= 2:
            prefixes.add(f"{parts[0]}/{parts[1]}")
    return sorted(prefixes)


def _resolve_remote_archive(
    job: Any, archive: str,
) -> Tuple[Path, archive_service.ArchiveListing]:
    executor = config_registry.executor_for_job(job)
    listing = archive_service.read_archive_listing(job.path, executor)
    archive_names = {item.filename for item in listing.archives}
    if archive not in archive_names:
        raise ValueError(f"Archive not found: {archive}")
    if not listing.target_base_dir:
        raise ValueError("Remote archive listing did not include target_base_dir.")
    return (
        _safe_archive_path(Path(listing.target_base_dir), archive),
        listing,
    )


def _build_remote_plan(
    job: Any,
    archive: str,
    *,
    selected_prefixes: Optional[List[str]] = None,
) -> Tuple[List[Any], List[Any]]:
    archive_path, _ = _resolve_remote_archive(job, archive)
    # overwrite=True for the dry-run so the remote planner reports every
    # conflict instead of bailing on the first one. The actual restore
    # honours the user's overwrite choice on the confirmation screen.
    result = config_registry.executor_for_job(job).restore_dry_run(
        job.path,
        archive_path,
        selected_prefixes=selected_prefixes,
        overwrite=True,
    )
    if not result.ok:
        error = result.payload.get("error") or result.raw_error.strip()
        raise ValueError(error or "Remote restore dry-run failed.")
    return result.payload.get("plan", []), result.payload.get("conflicts", [])


def _build_local_plan(
    selection: Any,
    archive: str,
    *,
    selected_prefixes: Optional[List[str]] = None,
) -> PlanView:
    _, config = _require_config(selection)
    zip_path = _safe_archive_path(config.target_base_dir, archive)
    if not zip_path.is_file():
        raise ValueError(f"Archive not found: {archive}")

    logger = logging.getLogger("archwright.restore.plan")
    logger.setLevel(logging.WARNING)

    from backup.restore import detect_conflicts, plan_restore

    plan = plan_restore(
        zip_path, config, logger, selected_prefixes=selected_prefixes,
    )
    conflicts = detect_conflicts(plan)
    prefixes = [
        f"{sf.folder_name}/{sf.subfolder_name}" for sf in config.subfolders
    ]
    return PlanView(
        plan=plan,
        conflicts=conflicts,
        prefixes=prefixes,
        config=config,
        is_remote=False,
    )


def _is_remote(selection: Any) -> bool:
    return (
        selection.selected is not None
        and selection.selected.executor != EXECUTOR_LOCAL
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_archives(selection: Any) -> ArchivesView:
    """Step 1: list available archives for the selected job."""
    if selection.selected is None:
        return ArchivesView(error=selection.error)

    job = selection.selected

    if job.executor != EXECUTOR_LOCAL:
        try:
            listing = archive_service.read_archive_listing(
                job.path,
                config_registry.executor_for_job(job),
            )
        except archive_service.ArchiveListError as exc:
            return ArchivesView(error=str(exc))
        return ArchivesView(
            archives=list(listing.archives),
            config=SimpleNamespace(target_base_dir=listing.target_base_dir),
        )

    if job.config is None:
        return ArchivesView(error=selection.error)

    archives, error = _list_local_archives(job.path)
    return ArchivesView(
        archives=archives,
        config=job.config,
        error=error,
    )


def build_plan(
    selection: Any,
    archive: str,
    *,
    selected_prefixes: Optional[List[str]] = None,
) -> PlanView:
    """Steps 2-4: build a restore plan for the selected job and archive.

    Raises ``ValueError`` on any planning error (caller decides whether to
    fall back to step 1 or render an inline error).
    """
    if selection.selected is None:
        raise ValueError(selection.error or "No job selected.")

    if _is_remote(selection):
        plan, conflicts = _build_remote_plan(
            selection.selected,
            archive,
            selected_prefixes=selected_prefixes,
        )
        return PlanView(
            plan=plan,
            conflicts=conflicts,
            prefixes=_prefixes_from_plan(plan),
            config=None,
            is_remote=True,
        )

    return _build_local_plan(
        selection, archive, selected_prefixes=selected_prefixes,
    )


def start_execution(
    selection: Any,
    archive: str,
    *,
    overwrite: bool = False,
    selected_prefixes: Optional[List[str]] = None,
) -> bool:
    """Step 5: start the actual restore via the job runner.

    Returns ``True`` if the job started, ``False`` if another job is
    already running. Raises ``ValueError`` if the local config cannot be
    resolved or the archive name is unsafe.
    """
    if selection.selected is None:
        raise ValueError(selection.error or "No job selected.")

    if _is_remote(selection):
        job = selection.selected
        archive_path, _ = _resolve_remote_archive(job, archive)
        executor = config_registry.executor_for_job(job)
        return runner.run_remote_restore(
            lambda on_line: executor.restore(
                job.path,
                archive_path,
                selected_prefixes=selected_prefixes,
                overwrite=overwrite,
                on_line=on_line,
            )
        )

    config_path, _ = _require_config(selection)
    return runner.run_restore(
        config_path=config_path,
        archive=archive,
        selected_prefixes=selected_prefixes,
        overwrite=overwrite,
    )
