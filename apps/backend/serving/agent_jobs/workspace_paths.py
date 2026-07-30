"""Stable, validated paths for durable agent-job workspaces."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os

_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class WorkspaceIdError(ValueError):
    """Raised when a workspace identifier cannot safely name a directory."""


def validate_workspace_id(raw: str | None) -> str:
    """Return a safe workspace id or reject it before filesystem use."""
    value = (raw or "").strip()
    if not _WORKSPACE_ID.fullmatch(value):
        raise WorkspaceIdError("workspace id must contain only letters, digits, '_' or '-'")
    return value


def workspace_path(root: str | os.PathLike[str], workspace_id: str) -> Path:
    """Resolve one workspace directly below ``root`` without path traversal."""
    safe_id = validate_workspace_id(workspace_id)
    base = Path(root).resolve()
    candidate = (base / safe_id).resolve(strict=False)
    if candidate.parent != base:
        raise WorkspaceIdError("workspace path escaped its configured root")
    return candidate


def purge_stale_workspaces(
    root: str | os.PathLike[str], *, ttl_seconds: float, now: float | None = None
) -> list[str]:
    """Remove expired durable worktrees without following attacker-made links."""
    if ttl_seconds <= 0:
        return []
    base = Path(root).resolve()
    if not base.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - ttl_seconds
    removed: list[str] = []
    for child in base.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            validate_workspace_id(child.name)
            modified = child.stat(follow_symlinks=False).st_mtime
        except (OSError, WorkspaceIdError):
            continue
        if modified >= cutoff:
            continue
        shutil.rmtree(child)
        removed.append(child.name)
    return removed


__all__ = [
    "WorkspaceIdError",
    "purge_stale_workspaces",
    "validate_workspace_id",
    "workspace_path",
]
