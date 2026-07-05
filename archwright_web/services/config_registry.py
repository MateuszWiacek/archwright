"""Config discovery and selection for the web UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backup.models import BackupConfig

from archwright_web.services import config_service
from archwright_web.services.executor import SSHExecutor, local_executor
from archwright_web.services.inventory import (
    EXECUTOR_LOCAL,
    EXECUTOR_SSH,
    InventoryNode,
    load_inventory,
)

SSH_DISCOVERY_TIMEOUT = 10


@dataclass(frozen=True)
class ConfigSource:
    config_path: Optional[Path] = None
    config_dir: Optional[Path] = None
    inventory_path: Optional[Path] = None

    @property
    def is_multi_config(self) -> bool:
        return self.config_dir is not None or self.inventory_path is not None

    @property
    def is_inventory(self) -> bool:
        return self.inventory_path is not None


@dataclass(frozen=True)
class ConfigJob:
    id: str
    path: Path
    config: Optional[BackupConfig]
    error: Optional[str] = None
    node_id: Optional[str] = None
    executor: str = EXECUTOR_LOCAL
    node: Optional[InventoryNode] = None


@dataclass(frozen=True)
class ConfigSelection:
    jobs: List[ConfigJob]
    selected: Optional[ConfigJob]
    error: Optional[str] = None
    is_multi_config: bool = False
    is_inventory: bool = False


def create_source(
    config_path: Optional[Path] = None,
    config_dir: Optional[Path] = None,
    inventory_path: Optional[Path] = None,
) -> ConfigSource:
    provided = sum(
        value is not None for value in (config_path, config_dir, inventory_path)
    )
    if provided != 1:
        raise ValueError(
            "Provide exactly one of config_path, config_dir, or inventory_path."
        )

    return ConfigSource(
        config_path=config_path.resolve() if config_path is not None else None,
        config_dir=config_dir.resolve() if config_dir is not None else None,
        inventory_path=(
            inventory_path.resolve() if inventory_path is not None else None
        ),
    )


def list_jobs(source: ConfigSource) -> List[ConfigJob]:
    if source.config_path is not None:
        return [_load_job("default", source.config_path)]

    if source.inventory_path is not None:
        return _list_inventory_jobs(source.inventory_path)

    if source.config_dir is None or not source.config_dir.is_dir():
        return []

    return _list_config_dir_jobs(source.config_dir)


def _list_config_dir_jobs(
    config_dir: Path,
    *,
    node_id: Optional[str] = None,
    executor: str = EXECUTOR_LOCAL,
    node: Optional[InventoryNode] = None,
    prefix: str = "",
) -> List[ConfigJob]:
    if not config_dir.is_dir():
        return []

    paths = sorted(
        path for path in config_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
    )
    used_ids: Dict[str, int] = {}
    jobs: List[ConfigJob] = []
    for path in paths:
        base_id = f"{prefix}{_slugify(path.stem)}"
        count = used_ids.get(base_id, 0) + 1
        used_ids[base_id] = count
        job_id = base_id if count == 1 else f"{base_id}-{count}"
        jobs.append(_load_job(
            job_id,
            path,
            node_id=node_id,
            executor=executor,
            node=node,
        ))
    return jobs


def _list_inventory_jobs(inventory_path: Path) -> List[ConfigJob]:
    try:
        inventory = load_inventory(inventory_path)
    except ValueError as exc:
        return [ConfigJob("inventory", inventory_path, None, str(exc))]

    jobs: List[ConfigJob] = []
    for node in inventory.nodes:
        if node.is_local:
            config_dir = node.local_config_dir
            local_jobs = _list_config_dir_jobs(
                config_dir,
                node_id=node.id,
                executor=node.executor,
                node=node,
                prefix=f"{node.id}:",
            )
            if local_jobs:
                jobs.extend(local_jobs)
            else:
                jobs.append(ConfigJob(
                    id=node.id,
                    path=config_dir,
                    config=None,
                    error=(
                        f"No YAML configs found for local inventory node "
                        f"'{node.id}' in {config_dir}"
                    ),
                    node_id=node.id,
                    executor=node.executor,
                    node=node,
                ))
        else:
            jobs.extend(_list_ssh_config_dir_jobs(node))
    return jobs


def _list_ssh_config_dir_jobs(node: InventoryNode) -> List[ConfigJob]:
    result = SSHExecutor(node, timeout=SSH_DISCOVERY_TIMEOUT).list_config_paths()
    if not result.ok:
        error = result.payload.get("error") or result.raw_error.strip()
        return [ConfigJob(
            id=node.id,
            path=Path(node.config_dir),
            config=None,
            error=(
                f"Cannot discover YAML configs for SSH inventory node "
                f"'{node.id}' in {node.config_dir}: {error}"
            ),
            node_id=node.id,
            executor=node.executor,
            node=node,
        )]

    paths = [
        Path(item)
        for item in result.payload.get("configs", [])
        if isinstance(item, str) and item
    ]
    if not paths:
        return [ConfigJob(
            id=node.id,
            path=Path(node.config_dir),
            config=None,
            error=(
                f"No YAML configs found for SSH inventory node "
                f"'{node.id}' in {node.config_dir}"
            ),
            node_id=node.id,
            executor=node.executor,
            node=node,
        )]

    used_ids: Dict[str, int] = {}
    jobs: List[ConfigJob] = []
    for path in sorted(paths):
        base_id = f"{node.id}:{_slugify(path.stem)}"
        count = used_ids.get(base_id, 0) + 1
        used_ids[base_id] = count
        job_id = base_id if count == 1 else f"{base_id}-{count}"
        jobs.append(ConfigJob(
            id=job_id,
            path=path,
            config=None,
            error=None,
            node_id=node.id,
            executor=node.executor,
            node=node,
        ))
    return jobs


def select_job(source: ConfigSource, requested_job_id: Optional[str]) -> ConfigSelection:
    jobs = list_jobs(source)
    if not jobs:
        if source.config_dir is not None:
            error = f"No YAML configs found in {source.config_dir}"
        else:
            error = "No config file configured."
        return ConfigSelection([], None, error, source.is_multi_config, source.is_inventory)

    selected: Optional[ConfigJob]
    error: Optional[str] = None
    if requested_job_id:
        selected = next((job for job in jobs if job.id == requested_job_id), None)
        if selected is None:
            error = f"Unknown config job: {requested_job_id}"
    else:
        selected = jobs[0]

    if selected is not None and selected.error:
        error = selected.error

    return ConfigSelection(
        jobs,
        selected,
        error,
        source.is_multi_config,
        source.is_inventory,
    )


def require_config(selection: ConfigSelection) -> Tuple[Path, BackupConfig]:
    if selection.error:
        raise ValueError(selection.error)
    if selection.selected is None:
        raise ValueError("No config selected.")
    if selection.selected.config is None:
        raise ValueError(selection.selected.error or "Selected config is invalid.")
    return selection.selected.path, selection.selected.config


def require_job(selection: ConfigSelection) -> ConfigJob:
    if selection.error:
        raise ValueError(selection.error)
    if selection.selected is None:
        raise ValueError("No config selected.")
    if selection.selected.error:
        raise ValueError(selection.selected.error)
    return selection.selected


def executor_for_job(job: ConfigJob):
    if job.executor == EXECUTOR_SSH:
        if job.node is None:
            raise ValueError(f"SSH job '{job.id}' is missing inventory node data.")
        return SSHExecutor(job.node)
    return local_executor


def template_context(selection: ConfigSelection) -> Dict[str, object]:
    selected = selection.selected
    job_id = selected.id if selected is not None else ""
    use_job_query = selection.is_multi_config and bool(job_id)

    return {
        "jobs": selection.jobs,
        "selected_job": selected,
        "selected_job_id": job_id,
        "is_multi_config": selection.is_multi_config,
        "is_inventory": selection.is_inventory,
        "job_query": f"?job={job_id}" if use_job_query else "",
        "job_query_amp": f"&job={job_id}" if use_job_query else "",
        "config_path": str(selected.path) if selected is not None else "",
    }


def _load_job(
    job_id: str,
    path: Path,
    *,
    node_id: Optional[str] = None,
    executor: str = EXECUTOR_LOCAL,
    node: Optional[InventoryNode] = None,
) -> ConfigJob:
    try:
        return ConfigJob(
            job_id,
            path,
            config_service.load(path),
            None,
            node_id,
            executor,
            node,
        )
    except ValueError as exc:
        return ConfigJob(job_id, path, None, str(exc), node_id, executor, node)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug or "config"
