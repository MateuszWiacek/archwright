"""Inventory parsing for planned multi-node control."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

EXECUTOR_LOCAL = "local"
EXECUTOR_SSH = "ssh"
VALID_EXECUTORS = {EXECUTOR_LOCAL, EXECUTOR_SSH}


@dataclass(frozen=True)
class InventoryNode:
    id: str
    executor: str
    config_dir: str
    host: Optional[str] = None
    user: Optional[str] = None
    port: int = 22
    command: str = "archwright"

    @property
    def is_local(self) -> bool:
        return self.executor == EXECUTOR_LOCAL

    @property
    def is_ssh(self) -> bool:
        return self.executor == EXECUTOR_SSH

    @property
    def local_config_dir(self) -> Path:
        if not self.is_local:
            raise ValueError(f"Node '{self.id}' is not a local executor.")
        return Path(self.config_dir)


@dataclass(frozen=True)
class Inventory:
    path: Path
    nodes: List[InventoryNode]

    def get_node(self, node_id: str) -> InventoryNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise ValueError(f"Unknown inventory node: {node_id}")


def load_inventory(path: Path) -> Inventory:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read inventory '{path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid inventory YAML '{path}': {exc}") from exc

    data = _require_mapping(raw, "inventory")
    raw_nodes = _require_mapping(data.get("nodes"), "nodes")
    if not raw_nodes:
        raise ValueError("inventory.nodes must contain at least one node")

    nodes = []
    for node_id, node_data in raw_nodes.items():
        if not isinstance(node_id, str):
            raise ValueError("inventory node ids must be strings")
        nodes.append(_parse_node(node_id, node_data))

    return Inventory(path=path.resolve(), nodes=nodes)


def _parse_node(node_id: str, raw: Any) -> InventoryNode:
    _validate_node_id(node_id)
    data = _require_mapping(raw, f"node '{node_id}'")

    executor = _required_string(data, "executor", node_id)
    if executor not in VALID_EXECUTORS:
        raise ValueError(
            f"node '{node_id}' has unsupported executor '{executor}'"
        )

    config_dir = _required_string(data, "config_dir", node_id)
    command = _optional_string(data, "command", "archwright", node_id)
    port = _optional_int(data, "port", 22, node_id)

    host = _optional_string(data, "host", None, node_id)
    user = _optional_string(data, "user", None, node_id)

    if executor == EXECUTOR_LOCAL:
        if host or user:
            raise ValueError(
                f"local node '{node_id}' must not define host or user"
            )
    else:
        if not host:
            raise ValueError(f"ssh node '{node_id}' requires host")
        if not user:
            raise ValueError(f"ssh node '{node_id}' requires user")

    return InventoryNode(
        id=node_id,
        executor=executor,
        config_dir=config_dir,
        host=host,
        user=user,
        port=port,
        command=command,
    )


def _require_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _required_string(data: Dict[str, Any], key: str, node_id: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"node '{node_id}' requires string field '{key}'")
    return value.strip()


def _optional_string(
    data: Dict[str, Any],
    key: str,
    default: Optional[str],
    node_id: str,
) -> Optional[str]:
    value = data.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"node '{node_id}' field '{key}' must be a string")
    return value.strip()


def _optional_int(
    data: Dict[str, Any],
    key: str,
    default: int,
    node_id: str,
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"node '{node_id}' field '{key}' must be an integer")
    if value <= 0:
        raise ValueError(f"node '{node_id}' field '{key}' must be > 0")
    return value


def _validate_node_id(node_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", node_id):
        raise ValueError(
            "inventory node ids may only contain letters, digits, '-' and '_'"
        )
