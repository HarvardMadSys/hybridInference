"""Internal broker for live agent workspaces.

The public gateway authenticates workspace owners and forwards only a validated
job id.  This service is deliberately separate from the gateway: it mounts
the runner's worktree volume and Docker socket, while the public-facing backend
does neither.  Commands and Git inspection therefore execute in the same
disposable sandbox boundary as the agent, never in the trusted broker process.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import re
import secrets
import stat
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from serving.agent_jobs.sandbox import SandboxBackend, SandboxSpec, build_backend_from_env
from serving.agent_jobs.workspace_browser import (
    MAX_FILE_PREVIEW_BYTES,
    WorkspacePathError,
    normalize_workspace_path,
)
from serving.agent_jobs.workspace_paths import WorkspaceIdError, workspace_path

MAX_FILE_WRITE_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 5000
DEFAULT_COMMAND_TIMEOUT_S = 60.0
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class FileWriteRequest(BaseModel):
    """One bounded UTF-8 file replacement."""

    content: str = Field(..., max_length=MAX_FILE_WRITE_BYTES)


class TerminalRequest(BaseModel):
    """One interactive-shell command against the mounted workspace."""

    command: str = Field(..., min_length=1, max_length=8000)
    cwd: str = Field("/workspace", min_length=1, max_length=4096)
    timeout_seconds: float = Field(DEFAULT_COMMAND_TIMEOUT_S, gt=0, le=120)


def _workspace(root: str, workspace_id: str) -> Path:
    try:
        path = workspace_path(root, workspace_id)
    except WorkspaceIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_dir() or not (path / ".git").is_dir():
        raise HTTPException(status_code=404, detail="workspace is not materialized")
    os.utime(path, None)
    return path


def _relative_path(raw: str | None) -> str:
    try:
        return normalize_workspace_path(raw)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _open_parent_fd(root: Path, relative: str) -> tuple[int, str | None]:
    """Open a path's real parent beneath ``root`` without following symlinks."""
    parts = relative.split("/") if relative else []
    current_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=400, detail="workspace path crosses an invalid directory"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1] if parts else None
    except BaseException:
        os.close(current_fd)
        raise


def _directory_response(relative: str, directory_fd: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise HTTPException(status_code=409, detail="workspace directory cannot be read") from exc
    children: list[tuple[str, os.stat_result]] = []
    for name in names:
        if name == ".git":
            continue
        try:
            child_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            # The agent may be changing the tree while it is open in the UI.
            continue
        children.append((name, child_info))
    children.sort(key=lambda child: (not stat.S_ISDIR(child[1].st_mode), child[0].casefold()))
    for name, child_info in children[:MAX_DIRECTORY_ENTRIES]:
        child_relative = f"{relative}/{name}" if relative else name
        kind = (
            "symlink"
            if stat.S_ISLNK(child_info.st_mode)
            else "directory"
            if stat.S_ISDIR(child_info.st_mode)
            else "file"
        )
        entries.append(
            {
                "name": name,
                "path": child_relative,
                "kind": kind,
                "size": child_info.st_size if kind != "directory" else None,
                "binary": False,
                "truncated": False,
                "status": None,
                "omitted_reason": "symlink" if kind == "symlink" else None,
            }
        )
    return {
        "path": relative,
        "kind": "directory",
        "entries": entries,
        "writable": True,
        "source": "workspace",
    }


def _file_response(root: Path, relative: str) -> dict[str, Any]:
    parent_fd, name = _open_parent_fd(root, relative)
    try:
        info = (
            os.fstat(parent_fd)
            if name is None
            else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        )
    except FileNotFoundError as exc:
        os.close(parent_fd)
        raise HTTPException(status_code=404, detail="workspace path does not exist") from exc
    except OSError as exc:
        os.close(parent_fd)
        raise HTTPException(status_code=409, detail="workspace path cannot be read") from exc

    if stat.S_ISLNK(info.st_mode):
        os.close(parent_fd)
        return {
            "path": relative,
            "kind": "symlink",
            "size": info.st_size,
            "writable": False,
            "source": "workspace",
            "omitted_reason": "symlink",
        }
    if stat.S_ISDIR(info.st_mode):
        try:
            directory_fd = (
                parent_fd
                if name is None
                else os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            )
        except OSError as exc:
            os.close(parent_fd)
            raise HTTPException(
                status_code=409, detail="workspace directory cannot be read"
            ) from exc
        if name is not None:
            os.close(parent_fd)
        try:
            return _directory_response(relative, directory_fd)
        finally:
            os.close(directory_fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(parent_fd)
        raise HTTPException(status_code=409, detail="workspace path is not a regular file")

    if info.st_size > MAX_FILE_PREVIEW_BYTES:
        os.close(parent_fd)
        return {
            "path": relative,
            "kind": "file",
            "content": None,
            "size": info.st_size,
            "binary": False,
            "truncated": True,
            "writable": False,
            "source": "workspace",
            "omitted_reason": "too_large",
        }
    try:
        file_fd = os.open(name or ".", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise HTTPException(status_code=409, detail="workspace file cannot be read") from exc
    os.close(parent_fd)
    try:
        opened_info = os.fstat(file_fd)
        if not stat.S_ISREG(opened_info.st_mode):
            raise HTTPException(status_code=409, detail="workspace path is not a regular file")
        raw = b""
        while len(raw) <= MAX_FILE_PREVIEW_BYTES:
            chunk = os.read(file_fd, min(64 * 1024, MAX_FILE_PREVIEW_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(file_fd)
    if len(raw) > MAX_FILE_PREVIEW_BYTES:
        return {
            "path": relative,
            "kind": "file",
            "content": None,
            "size": opened_info.st_size,
            "binary": False,
            "truncated": True,
            "writable": False,
            "source": "workspace",
            "omitted_reason": "too_large",
        }
    binary = b"\x00" in raw
    try:
        content = None if binary else raw.decode("utf-8")
    except UnicodeDecodeError:
        binary = True
        content = None
    return {
        "path": relative,
        "kind": "file",
        "content": content,
        "size": len(raw),
        "binary": binary,
        "truncated": False,
        "writable": not binary,
        "source": "workspace",
        "omitted_reason": "binary" if binary else None,
    }


def _write_file(root: Path, relative: str, content: str) -> dict[str, Any]:
    if not relative:
        raise HTTPException(status_code=400, detail="a file path is required")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds the workspace write limit")

    parts = relative.split("/")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = root_fd
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except (FileNotFoundError, NotADirectoryError, OSError) as exc:
                raise HTTPException(
                    status_code=400, detail="file parent must be an existing real directory"
                ) from exc
            opened.append(next_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o644,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise HTTPException(status_code=409, detail="workspace file cannot be written") from exc
        try:
            with os.fdopen(file_fd, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(file_fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)
    os.utime(root, None)
    return _file_response(root, relative)


def _run_sandbox(
    backend: SandboxBackend,
    *,
    workdir: Path,
    script: str,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[int, str, str]:
    """Run a bounded command and return status, stdout and stderr."""
    process = backend.spawn(
        SandboxSpec(
            argv=["/bin/sh", "-lc", script],
            workdir=str(workdir),
            env={**backend.base_env(), "GIT_CONFIG_NOSYSTEM": "1", **env},
            phase="agent",
        )
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in process.lines():
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True, name="workspace-command").start()
    deadline = time.monotonic() + timeout_s
    chunks: list[str] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            process.kill()
            raise HTTPException(status_code=408, detail="workspace command timed out")
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            break
        size += len(line.encode("utf-8", errors="replace"))
        if size > MAX_COMMAND_OUTPUT_BYTES:
            process.kill()
            raise HTTPException(status_code=413, detail="workspace command output is too large")
        chunks.append(line)
    exit_code = process.wait()
    stderr = process.stderr_text()
    if len(stderr.encode("utf-8", errors="replace")) > MAX_COMMAND_OUTPUT_BYTES:
        stderr = stderr[-MAX_COMMAND_OUTPUT_BYTES:]
    os.utime(workdir, None)
    return exit_code, "".join(chunks), stderr


def _run_terminal(
    backend: SandboxBackend, workdir: Path, request: TerminalRequest
) -> dict[str, Any]:
    marker = secrets.token_hex(16)
    script = (
        'cd "$WORKSPACE_CWD" 2>/dev/null || { printf "invalid working directory\\n" >&2; '
        "exit 125; }; "
        'eval "$WORKSPACE_COMMAND"; status=$?; '
        f'printf "\\n__HYI_{marker}_CWD__%s\\n__HYI_{marker}_STATUS__%s\\n" "$PWD" "$status"'
    )
    process_status, stdout, stderr = _run_sandbox(
        backend,
        workdir=workdir,
        script=script,
        env={"WORKSPACE_COMMAND": request.command, "WORKSPACE_CWD": request.cwd},
        timeout_s=request.timeout_seconds,
    )
    cwd_marker = f"\n__HYI_{marker}_CWD__"
    status_marker = f"\n__HYI_{marker}_STATUS__"
    cwd = request.cwd
    status = process_status
    marker_index = stdout.rfind(cwd_marker)
    if marker_index >= 0:
        trailer = stdout[marker_index + len(cwd_marker) :]
        stdout = stdout[:marker_index]
        cwd_line, separator, status_text = trailer.partition(status_marker)
        if separator:
            cwd = cwd_line.strip() or request.cwd
            try:
                status = int(status_text.strip().splitlines()[0])
            except (ValueError, IndexError):
                status = process_status
    return {"output": stdout, "stderr": stderr, "exit_code": status, "cwd": cwd}


def _run_git(backend: SandboxBackend, workdir: Path, base_sha: str | None = None) -> dict[str, Any]:
    if base_sha and not _COMMIT_SHA.fullmatch(base_sha):
        raise HTTPException(status_code=400, detail="base SHA is not a commit hash")
    target = base_sha or "HEAD"
    marker = secrets.token_hex(16)
    status_tag = f"__HYI_{marker}_STATUS__"
    diff_tag = f"__HYI_{marker}_DIFF__"
    commits_tag = f"__HYI_{marker}_COMMITS__"
    script = (
        "git --no-pager status --porcelain=v1 --branch; status_status=$?; "
        f"printf '\n{diff_tag}\n'; "
        'index_path="${TMPDIR:-/tmp}/hyi-browser-index.$$"; '
        'rm -f "$index_path"; trap \'rm -f "$index_path"\' EXIT; '
        f'GIT_INDEX_FILE="$index_path" git read-tree {target} && '
        'GIT_INDEX_FILE="$index_path" git add -A -N >/dev/null 2>&1 && '
        f'GIT_INDEX_FILE="$index_path" git --no-pager diff '
        f"--no-ext-diff --no-textconv --binary {target}; diff_status=$?; "
        f"printf '\n{commits_tag}\n'; "
        "git --no-pager log -20 --pretty=format:'%H%x09%h%x09%s%x09%an%x09%aI'; "
        "log_status=$?; overall_status=$status_status; "
        '[ "$overall_status" -eq 0 ] && overall_status=$diff_status; '
        '[ "$overall_status" -eq 0 ] && overall_status=$log_status; '
        f"printf '\n{status_tag}%s\n' \"$overall_status\""
    )
    process_status, stdout, stderr = _run_sandbox(
        backend, workdir=workdir, script=script, env={}, timeout_s=60
    )
    status_text, separator, remainder = stdout.partition(f"\n{diff_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    patch, separator, commits_and_status = remainder.partition(f"\n{commits_tag}\n")
    if not separator:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")
    commits_text, _, git_status_text = commits_and_status.rpartition(f"\n{status_tag}")
    try:
        git_status = int(git_status_text.strip().splitlines()[0])
    except (ValueError, IndexError):
        git_status = process_status
    if git_status != 0:
        raise HTTPException(status_code=502, detail=stderr or "git workspace inspection failed")

    status_lines = [line for line in status_text.splitlines() if line]
    branch = ""
    if status_lines and status_lines[0].startswith("## "):
        branch = status_lines.pop(0)[3:].split("...", 1)[0]
    changes = [{"code": line[:2], "path": line[3:]} for line in status_lines if len(line) >= 4]
    commits = []
    for line in commits_text.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            commits.append(
                {
                    "sha": parts[0],
                    "short_sha": parts[1],
                    "subject": parts[2],
                    "author": parts[3],
                    "authored_at": parts[4],
                }
            )
    return {
        "available": True,
        "branch": branch,
        "changes": changes,
        "patch": patch,
        "commits": commits,
    }


def create_app(
    *,
    backend: SandboxBackend | None = None,
    workdir_root: str | None = None,
    token: str | None = None,
) -> FastAPI:
    """Create the internal broker app with injectable dependencies for tests."""
    sandbox = backend or build_backend_from_env()
    root = workdir_root or os.environ.get(
        "AGENT_WORKDIR_ROOT", "/var/lib/hybridinference/agent-jobs"
    )
    expected_token = (
        token if token is not None else os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN", "")
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Path(root).mkdir(parents=True, exist_ok=True)
        sandbox.preflight(workdir_root=root)
        yield

    app = FastAPI(title="Agent workspace broker", docs_url=None, redoc_url=None, lifespan=lifespan)

    def authenticate(x_agent_workspace_token: str | None = Header(None)) -> None:
        if (
            not expected_token
            or not x_agent_workspace_token
            or not secrets.compare_digest(expected_token, x_agent_workspace_token)
        ):
            raise HTTPException(status_code=401, detail="invalid workspace broker credential")

    @app.get("/health")
    def health(_: None = Depends(authenticate)) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/workspaces/{workspace_id}/files")
    def files(
        workspace_id: str,
        path: str = Query("", max_length=4096),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        workdir = _workspace(root, workspace_id)
        return _file_response(workdir, _relative_path(path))

    @app.put("/workspaces/{workspace_id}/files")
    def write_file(
        workspace_id: str,
        body: FileWriteRequest,
        path: str = Query(..., min_length=1, max_length=4096),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        workdir = _workspace(root, workspace_id)
        return _write_file(workdir, _relative_path(path), body.content)

    @app.post("/workspaces/{workspace_id}/terminal")
    async def terminal(
        workspace_id: str,
        body: TerminalRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        workdir = _workspace(root, workspace_id)
        return await asyncio.to_thread(_run_terminal, sandbox, workdir, body)

    @app.get("/workspaces/{workspace_id}/git")
    async def git(
        workspace_id: str,
        base_sha: str | None = Query(None, max_length=64),
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        workdir = _workspace(root, workspace_id)
        return await asyncio.to_thread(_run_git, sandbox, workdir, base_sha)

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    """Run the broker on the private Compose network."""
    parser = argparse.ArgumentParser(description="Serve durable agent workspaces.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FileWriteRequest",
    "TerminalRequest",
    "app",
    "create_app",
]
