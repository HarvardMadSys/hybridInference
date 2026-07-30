"""Bounded, symlink-safe snapshots of files changed by an agent job."""

from __future__ import annotations

import errno
import json
import os
import pathlib
import re
import stat
from typing import Any

from serving.agent_jobs.patch_gate import MAX_CHANGED_FILES, validate_patch

SNAPSHOT_VERSION = 1
DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = MAX_CHANGED_FILES
DEFAULT_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class _UnsafePath(ValueError):
    """The requested path cannot be read without leaving the worktree."""


class _SymlinkPath(ValueError):
    """The requested path contains a symlink."""

    def __init__(self, name: str, *, size: int) -> None:
        super().__init__(name)
        self.size = size


def _path_parts(path: str) -> tuple[str, ...]:
    raw_parts = path.split("/")
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or re.match(r"^[A-Za-z]:/", path)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise _UnsafePath(path)
    candidate = pathlib.PurePosixPath(path)
    parts = candidate.parts
    if candidate.is_absolute():
        raise _UnsafePath(path)
    if any(part.casefold() == ".git" for part in parts):
        raise _UnsafePath(path)
    return parts


def _symlink_size(parent_fd: int, name: str) -> int | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISLNK(info.st_mode) else None


def _open_changed_file(root_fd: int, parts: tuple[str, ...]) -> tuple[int, os.stat_result]:
    """Open one regular file beneath ``root_fd`` without following links."""
    directory_fd = os.dup(root_fd)
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                symlink_size = _symlink_size(directory_fd, component)
                if exc.errno in {errno.ELOOP, errno.ENOTDIR} and symlink_size is not None:
                    raise _SymlinkPath(component, size=symlink_size) from exc
                raise
            os.close(directory_fd)
            directory_fd = next_fd

        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        except OSError as exc:
            symlink_size = _symlink_size(directory_fd, parts[-1])
            if exc.errno in {errno.ELOOP, errno.ENOTDIR} and symlink_size is not None:
                raise _SymlinkPath(parts[-1], size=symlink_size) from exc
            raise
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise _UnsafePath("not a regular file")
        return file_fd, info
    finally:
        os.close(directory_fd)


def _added_paths(patch: str) -> set[str]:
    """Return paths represented as newly added in a git patch."""
    added: set[str] = set()
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    for section in sections:
        if not any(
            marker in section
            for marker in ("new file mode ", "\n--- /dev/null\n", "\nrename to ", "\ncopy to ")
        ):
            continue
        added.update(validate_patch(section).changed_files)
    return added


def _omitted_entry(path: str, status: str, size: int, reason: str) -> dict[str, Any]:
    return {"path": path, "status": status, "size": size, "omitted": reason}


def build_workspace_snapshot(
    workdir: str,
    patch: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> str:
    """Build a bounded JSON overlay for files named by an agent's patch.

    Paths are opened component-by-component relative to a descriptor for the
    worktree. No symlink is followed, including one in an intermediate
    directory, and special files are never read.
    """
    if min(max_file_bytes, max_total_bytes, max_files, max_artifact_bytes) < 0:
        raise ValueError("workspace snapshot limits must be non-negative")

    changed_paths = validate_patch(patch).changed_files if patch.strip() else []
    added_paths = _added_paths(patch)
    selected_paths = changed_paths[:max_files]
    truncated = len(changed_paths) > len(selected_paths)
    files: list[dict[str, Any]] = []
    read_bytes = 0
    skipped = 0

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(workdir, root_flags)
    try:
        for path in selected_paths:
            try:
                parts = _path_parts(path)
            except _UnsafePath:
                skipped += 1
                truncated = True
                continue

            status = "added" if path in added_paths else "modified"
            try:
                file_fd, info = _open_changed_file(root_fd, parts)
            except FileNotFoundError:
                files.append({"path": path, "status": "deleted", "size": 0})
                continue
            except _SymlinkPath as exc:
                files.append(_omitted_entry(path, status, exc.size, "symlink"))
                truncated = True
                continue
            except (OSError, _UnsafePath):
                files.append(_omitted_entry(path, status, 0, "unreadable"))
                truncated = True
                continue

            size = info.st_size
            try:
                if size > max_file_bytes:
                    files.append(_omitted_entry(path, status, size, "too_large"))
                    truncated = True
                    continue
                remaining_total = max_total_bytes - read_bytes
                if size > remaining_total:
                    files.append(_omitted_entry(path, status, size, "total_limit"))
                    truncated = True
                    continue

                data = b""
                read_limit = min(max_file_bytes, remaining_total)
                while len(data) < read_limit:
                    chunk = os.read(file_fd, min(64 * 1024, read_limit - len(data)))
                    if not chunk:
                        break
                    data += chunk
                read_bytes += len(data)
                final_size = os.fstat(file_fd).st_size
                if final_size > len(data):
                    reason = "too_large" if final_size > max_file_bytes else "total_limit"
                    files.append(_omitted_entry(path, status, final_size, reason))
                    truncated = True
                    continue
                if b"\x00" in data:
                    files.append(_omitted_entry(path, status, final_size, "binary"))
                    truncated = True
                    continue
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    files.append(_omitted_entry(path, status, final_size, "binary"))
                    truncated = True
                    continue
                files.append(
                    {"path": path, "status": status, "size": final_size, "content": content}
                )
            finally:
                os.close(file_fd)
    finally:
        os.close(root_fd)

    snapshot = {
        "version": SNAPSHOT_VERSION,
        "files": files,
        "truncated": truncated,
        "limits": {
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "max_files": max_files,
            "max_artifact_bytes": max_artifact_bytes,
        },
    }
    if skipped:
        snapshot["skipped"] = skipped
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= max_artifact_bytes:
        return encoded

    snapshot["truncated"] = True
    for item in reversed(files):
        if "content" in item:
            item.pop("content")
            item["omitted"] = "artifact_limit"
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    while files and len(encoded.encode("utf-8")) > max_artifact_bytes:
        files.pop()
        skipped += 1
        snapshot["skipped"] = skipped
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_artifact_bytes:
        raise ValueError("workspace snapshot metadata exceeds the artifact limit")
    return encoded


__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "SNAPSHOT_VERSION",
    "build_workspace_snapshot",
]
