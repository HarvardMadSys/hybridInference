"""Safe, bounded helpers for the owner-facing agent workspace browser."""

from __future__ import annotations

import base64
import json
from pathlib import PurePosixPath
from typing import Any

MAX_FILE_PREVIEW_BYTES = 512 * 1024
MAX_SNAPSHOT_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_FILES = 5000


class WorkspacePathError(ValueError):
    """A workspace path is not a safe canonical repository-relative path."""


class WorkspaceSnapshotError(ValueError):
    """A stored workspace snapshot is malformed or exceeds the reader bounds."""


def normalize_workspace_path(raw: str | None) -> str:
    """Validate and return a canonical POSIX path, or ``""`` for the root.

    Reject instead of normalizing aliases such as ``a/../b``.  That keeps one
    cache and authorization identity per path and makes encoded traversal fail
    closed after FastAPI has URL-decoded the query parameter.
    """
    if raw is None or raw == "":
        return ""
    if (
        len(raw) > 4096
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("/")
        or (len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == ":/")
    ):
        raise WorkspacePathError("path must be a safe repository-relative path")
    parts = raw.split("/")
    if any(not part or part in {".", ".."} or part.casefold() == ".git" for part in parts):
        raise WorkspacePathError("path must be a safe repository-relative path")
    path = PurePosixPath(*parts).as_posix()
    if path != raw:
        raise WorkspacePathError("path must use canonical POSIX encoding")
    return path


def parse_workspace_snapshot(content: str | None) -> dict[str, dict[str, Any]]:
    """Parse the bounded v1 changed-file artifact into a path keyed overlay."""
    if not content:
        return {}
    if len(content.encode("utf-8")) > MAX_SNAPSHOT_ARTIFACT_BYTES:
        raise WorkspaceSnapshotError("workspace snapshot exceeds the reader limit")
    try:
        body = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkspaceSnapshotError("workspace snapshot is not valid JSON") from exc
    if not isinstance(body, dict) or body.get("version") != 1:
        raise WorkspaceSnapshotError("unsupported workspace snapshot version")
    files = body.get("files")
    if not isinstance(files, list) or len(files) > MAX_SNAPSHOT_FILES:
        raise WorkspaceSnapshotError("workspace snapshot has an invalid file list")

    overlay: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise WorkspaceSnapshotError("workspace snapshot contains an invalid file")
        try:
            path = normalize_workspace_path(item["path"])
        except WorkspacePathError as exc:
            raise WorkspaceSnapshotError("workspace snapshot contains an unsafe path") from exc
        if not path or item.get("status") not in {"added", "modified", "deleted"}:
            raise WorkspaceSnapshotError("workspace snapshot contains an invalid status")
        size = item.get("size", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise WorkspaceSnapshotError("workspace snapshot contains an invalid size")
        value = dict(item)
        value["path"] = path
        value["size"] = size
        content_value = value.get("content")
        if content_value is not None and not isinstance(content_value, str):
            raise WorkspaceSnapshotError("workspace snapshot contains invalid content")
        overlay[path] = value
    return overlay


def _public_snapshot_file(item: dict[str, Any]) -> dict[str, Any]:
    omitted = item.get("omitted")
    deleted = item["status"] == "deleted"
    return {
        "path": item["path"],
        "kind": "symlink" if omitted == "symlink" else "file",
        "content": None if deleted or omitted else item.get("content", ""),
        "size": item.get("size", 0),
        "binary": omitted == "binary",
        "truncated": omitted not in {None, "binary", "symlink"} and not deleted,
        "status": item["status"],
        "omitted_reason": omitted,
    }


def snapshot_file_response(overlay: dict[str, dict[str, Any]], path: str) -> dict[str, Any] | None:
    """Return an overlay file response when the exact path was changed."""
    item = overlay.get(path)
    return _public_snapshot_file(item) if item is not None else None


def _entry_from_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    public = _public_snapshot_file(item)
    return {
        "name": item["path"].rsplit("/", 1)[-1],
        "path": item["path"],
        "kind": public["kind"],
        "size": public["size"],
        "binary": public["binary"],
        "truncated": public["truncated"],
        "status": public["status"],
        "omitted_reason": public["omitted_reason"],
    }


def merge_directory_entries(
    *,
    path: str,
    baseline: list[dict[str, Any]],
    overlay: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge a GitHub directory listing with direct children from the snapshot."""
    entries: dict[str, dict[str, Any]] = {}
    prefix = f"{path}/" if path else ""
    for item in baseline:
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        try:
            item_path = normalize_workspace_path(raw_path)
        except WorkspacePathError:
            continue
        if not item_path.startswith(prefix):
            continue
        remainder = item_path[len(prefix) :]
        if not remainder or "/" in remainder:
            continue
        raw_type = item.get("type")
        kind = "directory" if raw_type == "dir" else "symlink" if raw_type == "symlink" else "file"
        entries[item_path] = {
            "name": remainder,
            "path": item_path,
            "kind": kind,
            "size": item.get("size") if isinstance(item.get("size"), int) else None,
            "binary": False,
            "truncated": False,
            "status": None,
            "omitted_reason": "symlink" if kind == "symlink" else None,
        }

    for item_path, item in overlay.items():
        if not item_path.startswith(prefix):
            continue
        remainder = item_path[len(prefix) :]
        if not remainder:
            continue
        child = remainder.split("/", 1)[0]
        child_path = f"{prefix}{child}"
        if "/" in remainder:
            entry = entries.get(child_path)
            if entry is None or entry["kind"] != "directory":
                entries[child_path] = {
                    "name": child,
                    "path": child_path,
                    "kind": "directory",
                    "size": None,
                    "binary": False,
                    "truncated": False,
                    "status": (
                        "added" if entry is None and item["status"] == "added" else "modified"
                    ),
                    "omitted_reason": None,
                }
            elif entry["status"] is None:
                entry["status"] = "modified"
            continue
        entries[child_path] = _entry_from_snapshot(item)

    return sorted(
        entries.values(), key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold())
    )


def github_file_response(path: str, item: dict[str, Any]) -> dict[str, Any]:
    """Decode a bounded UTF-8 preview from one GitHub Contents API object."""
    raw_type = item.get("type")
    size = item.get("size") if isinstance(item.get("size"), int) else 0
    if raw_type == "symlink":
        return {
            "path": path,
            "kind": "symlink",
            "content": None,
            "size": size,
            "binary": False,
            "truncated": False,
            "status": None,
            "omitted_reason": "symlink",
        }
    if raw_type != "file":
        raise WorkspaceSnapshotError("GitHub returned neither a file nor a directory")
    if size > MAX_FILE_PREVIEW_BYTES:
        return {
            "path": path,
            "kind": "file",
            "content": None,
            "size": size,
            "binary": False,
            "truncated": True,
            "status": None,
            "omitted_reason": "too_large",
        }
    encoded = item.get("content")
    if item.get("encoding") != "base64" or not isinstance(encoded, str):
        return {
            "path": path,
            "kind": "file",
            "content": None,
            "size": size,
            "binary": False,
            "truncated": True,
            "status": None,
            "omitted_reason": "unavailable",
        }
    try:
        # GitHub wraps base64 payloads with newlines.  Remove only ASCII
        # whitespace, then retain strict validation for every other byte.
        compact = encoded.translate(str.maketrans("", "", " \t\r\n\f\v"))
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise WorkspaceSnapshotError("GitHub returned malformed base64 file content") from exc
    if len(raw) > MAX_FILE_PREVIEW_BYTES:
        return {
            "path": path,
            "kind": "file",
            "content": None,
            "size": size or len(raw),
            "binary": False,
            "truncated": True,
            "status": None,
            "omitted_reason": "too_large",
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    binary = "\x00" in text or (not text and bool(raw))
    return {
        "path": path,
        "kind": "file",
        "content": None if binary else text,
        "size": size or len(raw),
        "binary": binary,
        "truncated": False,
        "status": None,
        "omitted_reason": "binary" if binary else None,
    }


def overlay_has_directory(overlay: dict[str, dict[str, Any]], path: str) -> bool:
    """Whether changed files synthesize a directory absent from the base tree."""
    prefix = f"{path}/" if path else ""
    return any(item_path.startswith(prefix) and item_path != path for item_path in overlay)
