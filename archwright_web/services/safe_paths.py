"""Path safety helpers for web routes."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def safe_child_path(
    base_dir: Path,
    filename: str,
    *,
    label: str,
    required_suffix: Optional[str] = None,
) -> Path:
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or "\x00" in filename
    ):
        raise ValueError(f"Invalid {label}: {filename}")
    if required_suffix and not filename.endswith(required_suffix):
        raise ValueError(f"Invalid {label}: {filename}")

    resolved_base = base_dir.resolve()
    resolved = (base_dir / filename).resolve()
    if resolved.parent != resolved_base:
        raise ValueError(f"{label.capitalize()} escapes target directory: {filename}")
    return resolved
