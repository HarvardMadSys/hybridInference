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
import base64
import binascii
import contextlib
import json
import os
import queue
import re
import secrets
import stat
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from serving.agent_jobs.sandbox import (
    SandboxBackend,
    SandboxError,
    SandboxSpec,
    TerminalNotSupportedError,
    TerminalProcess,
    build_backend_from_env,
)
from serving.agent_jobs.workspace_browser import (
    MAX_FILE_PREVIEW_BYTES,
    WorkspacePathError,
    normalize_workspace_path,
)
from serving.agent_jobs.workspace_paths import WorkspaceIdError, workspace_path
from serving.utils.logging import get_logger

logger = get_logger(__name__)

MAX_FILE_WRITE_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 5000
DEFAULT_COMMAND_TIMEOUT_S = 60.0
MAX_TERMINAL_SESSIONS_PER_WORKSPACE = 4
DEFAULT_MAX_TERMINAL_SESSIONS = 64
MAX_TERMINAL_BUFFER_BYTES = 2 * 1024 * 1024
MAX_TERMINAL_BUFFER_EVENTS = 4096
MAX_TERMINAL_TOTAL_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_TERMINAL_INPUT_BYTES = 64 * 1024
MAX_TERMINAL_STREAMS_PER_SESSION = 4
DEFAULT_TERMINAL_IDLE_TTL_S = 30 * 60.0
DEFAULT_TERMINAL_HARD_TTL_S = 8 * 60 * 60.0
TERMINAL_STREAM_WAIT_S = 15.0
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class FileWriteRequest(BaseModel):
    """One bounded UTF-8 file replacement."""

    content: str = Field(..., max_length=MAX_FILE_WRITE_BYTES)


class TerminalRequest(BaseModel):
    """One interactive-shell command against the mounted workspace."""

    command: str = Field(..., min_length=1, max_length=8000)
    cwd: str = Field("/workspace", min_length=1, max_length=4096)
    timeout_seconds: float = Field(DEFAULT_COMMAND_TIMEOUT_S, gt=0, le=120)


class TerminalCreateRequest(BaseModel):
    """Dimensions for one persistent interactive terminal."""

    rows: int = Field(24, ge=2, le=200)
    cols: int = Field(80, ge=20, le=500)


class TerminalInputRequest(BaseModel):
    """One bounded base64-encoded terminal input chunk."""

    data: str = Field(..., min_length=1, max_length=88_000)


class TerminalResizeRequest(BaseModel):
    """A validated PTY window size."""

    rows: int = Field(..., ge=2, le=200)
    cols: int = Field(..., ge=20, le=500)


@dataclass(frozen=True)
class _TerminalEvent:
    """One resumable event in a terminal's bounded output ring."""

    seq: int
    kind: str
    data: bytes = b""
    exit_code: int | None = None
    reason: str | None = None


class _TerminalSession:
    """Thread-safe state and output history for one terminal process."""

    def __init__(
        self,
        *,
        terminal_id: str,
        process: TerminalProcess,
        workdir: Path,
        rows: int,
        cols: int,
        now: float,
    ) -> None:
        self.id = terminal_id
        self.shell = "sh"
        self.cwd = "/workspace"
        self.rows = rows
        self.cols = cols
        self.created_at = now
        self.last_active_at = now
        self.state = "running"
        self._process = process
        self._workdir = workdir
        self._events: deque[_TerminalEvent] = deque()
        self._buffer_bytes = 0
        self._last_seq = 0
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._closed = False
        self._total_output_bytes = 0
        self._active_streams = 0
        self._async_waiters: set[asyncio.Future[None]] = set()
        self._pump_thread = threading.Thread(
            target=self._pump,
            daemon=True,
            name=f"workspace-terminal-{terminal_id[-8:]}",
        )

    def start(self) -> None:
        """Start draining output immediately so the PTY can never back up."""
        self._pump_thread.start()

    def descriptor(self) -> dict[str, Any]:
        """Return the public, non-secret terminal state."""
        with self._condition:
            return {
                "id": self.id,
                "shell": self.shell,
                "state": self.state,
                "cwd": self.cwd,
                "rows": self.rows,
                "cols": self.cols,
                "last_seq": self._last_seq,
            }

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    def expired(self, *, now: float, idle_ttl_s: float, hard_ttl_s: float) -> bool:
        """Whether idle or absolute lifetime requires this session to close."""
        with self._condition:
            return not self._closed and (
                self.state == "cleanup_failed"
                or now - self.last_active_at >= idle_ttl_s
                or now - self.created_at >= hard_ttl_s
            )

    def write(self, data: bytes, *, now: float) -> dict[str, Any]:
        """Write terminal input, rejecting writes after exit/close."""
        with self._condition:
            if self.state != "running" or self._closed:
                raise HTTPException(status_code=409, detail="terminal is not running")
            self.last_active_at = now
        try:
            self._process.write(data)
        except SandboxError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        with contextlib.suppress(OSError):
            os.utime(self._workdir, None)
        return self.descriptor()

    def resize(self, *, rows: int, cols: int, now: float) -> dict[str, Any]:
        """Resize a running terminal and persist the accepted dimensions."""
        with self._condition:
            if self.state != "running" or self._closed:
                raise HTTPException(status_code=409, detail="terminal is not running")
            self.last_active_at = now
        try:
            self._process.resize(rows, cols)
        except SandboxError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        with self._condition:
            self.rows = rows
            self.cols = cols
        return self.descriptor()

    def close(self) -> dict[str, Any]:
        """Close the complete process tree, retaining failed cleanup for retry."""
        with self._close_lock:
            with self._condition:
                if self._closed:
                    return self.descriptor()
                self.state = "closing"
                self._condition.notify_all()
                self._wake_async_waiters_locked()
            try:
                self._process.kill()
            except (SandboxError, OSError):
                with self._condition:
                    self.state = "cleanup_failed"
                    self._condition.notify_all()
                    self._wake_async_waiters_locked()
                raise
            with self._condition:
                self._closed = True
                self.state = "closed"
                self._condition.notify_all()
                self._wake_async_waiters_locked()
            if threading.current_thread() is not self._pump_thread:
                self._pump_thread.join(timeout=5)
            return self.descriptor()

    def events_after(self, after: int, *, wait_s: float) -> list[_TerminalEvent]:
        """Return resumable events, waiting once when no newer event exists."""
        with self._condition:
            events = [event for event in self._events if event.seq > after]
            if not events and self.state == "running" and not self._closed:
                self._condition.wait(timeout=wait_s)
                events = [event for event in self._events if event.seq > after]
            self.last_active_at = time.monotonic()
            return events

    async def events_after_async(self, after: int, *, wait_s: float) -> list[_TerminalEvent]:
        """Wait for output without occupying Starlette's shared thread pool."""
        loop = asyncio.get_running_loop()
        with self._condition:
            events = [item for item in self._events if item.seq > after]
            if events or self.state != "running" or self._closed:
                self.last_active_at = time.monotonic()
                return events
            waiter = loop.create_future()
            self._async_waiters.add(waiter)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter, timeout=wait_s)
        finally:
            with self._condition:
                self._async_waiters.discard(waiter)
        with self._condition:
            self.last_active_at = time.monotonic()
            return [item for item in self._events if item.seq > after]

    def open_stream(self) -> None:
        """Bound duplicate viewers of one PTY before starting an SSE response."""
        with self._condition:
            if self._active_streams >= MAX_TERMINAL_STREAMS_PER_SESSION:
                raise HTTPException(status_code=429, detail="terminal stream limit reached")
            self._active_streams += 1

    def close_stream(self) -> None:
        """Release one SSE viewer slot."""
        with self._condition:
            self._active_streams = max(0, self._active_streams - 1)

    def _wake_async_waiters_locked(self) -> None:
        """Wake event-loop waiters from the PTY pump or cleanup threads."""
        waiters = tuple(self._async_waiters)
        self._async_waiters.clear()
        for waiter in waiters:
            loop = waiter.get_loop()

            def resolve(target: asyncio.Future[None] = waiter) -> None:
                if not target.done():
                    target.set_result(None)

            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(resolve)

    def _append_output(self, data: bytes) -> None:
        if not data:
            return
        chunk_truncated = len(data) > MAX_TERMINAL_BUFFER_BYTES
        if chunk_truncated:
            data = data[-MAX_TERMINAL_BUFFER_BYTES:]
        with self._condition:
            if chunk_truncated:
                # The retained tail is not a complete output event. Give the
                # reset its own cursor so reconnecting after it resumes at the
                # retained output instead of receiving the reset forever.
                self._events.clear()
                self._buffer_bytes = 0
                self._last_seq += 1
                self._events.append(
                    _TerminalEvent(
                        self._last_seq,
                        "reset",
                        reason="output_truncated",
                    )
                )
            self._last_seq += 1
            self._events.append(_TerminalEvent(self._last_seq, "output", data=data))
            self._buffer_bytes += len(data)
            self._trim_events_locked()
            self.last_active_at = time.monotonic()
            self._condition.notify_all()
            self._wake_async_waiters_locked()

    def _append_exit(self, exit_code: int) -> None:
        with self._condition:
            self._last_seq += 1
            self._events.append(_TerminalEvent(self._last_seq, "exit", exit_code=exit_code))
            self._trim_events_locked()
            if not self._closed and self.state == "running":
                self.state = "exited"
            self._condition.notify_all()
            self._wake_async_waiters_locked()

    def _trim_events_locked(self) -> None:
        """Bound the ring while preserving an explicit resumability gap marker."""
        reset_seq: int | None = None
        if self._events and self._events[0].kind == "reset":
            reset_seq = self._events.popleft().seq
        while self._events and (
            self._buffer_bytes > MAX_TERMINAL_BUFFER_BYTES
            or len(self._events) + (reset_seq is not None) > MAX_TERMINAL_BUFFER_EVENTS
        ):
            oldest = self._events.popleft()
            self._buffer_bytes -= len(oldest.data)
            reset_seq = oldest.seq
        if reset_seq is not None:
            self._events.appendleft(_TerminalEvent(reset_seq, "reset", reason="output_truncated"))

    def _pump(self) -> None:
        """Continuously drain the PTY and publish one terminal exit event."""
        try:
            for chunk in self._process.chunks():
                self._total_output_bytes += len(chunk)
                if self._total_output_bytes > MAX_TERMINAL_TOTAL_OUTPUT_BYTES:
                    self._append_output(
                        b"\r\n\x1b[31m[terminal stopped: output limit exceeded]\x1b[0m\r\n"
                    )
                    try:
                        self._process.kill()
                    except (SandboxError, OSError):
                        with self._condition:
                            self.state = "cleanup_failed"
                            self._condition.notify_all()
                            self._wake_async_waiters_locked()
                    break
                self._append_output(chunk)
                with contextlib.suppress(OSError):
                    os.utime(self._workdir, None)
        except Exception:
            logger.exception("workspace_terminal_output_failed")
            try:
                self._process.kill()
            except Exception:
                logger.exception("workspace_terminal_output_cleanup_failed")
                with self._condition:
                    self.state = "cleanup_failed"
                    self._condition.notify_all()
                    self._wake_async_waiters_locked()
        try:
            exit_code = self._process.wait()
        except Exception:
            logger.exception("workspace_terminal_wait_failed")
            exit_code = 1
        self._append_exit(exit_code)


class _TerminalManager:
    """Ephemeral, bounded registry of terminal sessions by workspace."""

    def __init__(
        self,
        backend: SandboxBackend,
        *,
        idle_ttl_s: float,
        hard_ttl_s: float,
        max_sessions: int,
    ) -> None:
        self._backend = backend
        self._idle_ttl_s = idle_ttl_s
        self._hard_ttl_s = hard_ttl_s
        self._max_sessions = max_sessions
        self._sessions: dict[str, dict[str, _TerminalSession]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def start(self) -> None:
        """Start periodic TTL cleanup once the app lifespan begins."""
        with self._lock:
            if self._reaper is not None:
                return
            self._reaper = threading.Thread(
                target=self._reap_loop,
                daemon=True,
                name="workspace-terminal-reaper",
            )
            self._reaper.start()

    def close(self) -> None:
        """Stop cleanup and kill every live sandbox during broker shutdown."""
        self._stop.set()
        with self._lock:
            sessions = [session for group in self._sessions.values() for session in group.values()]
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except (SandboxError, OSError):
                logger.exception("workspace_terminal_shutdown_cleanup_failed")
        if self._reaper is not None:
            self._reaper.join(timeout=5)

    def create(self, workspace_id: str, workdir: Path, *, rows: int, cols: int) -> dict[str, Any]:
        """Create one PTY while atomically enforcing the per-workspace cap."""
        self.reap()
        terminal_id = "term_" + secrets.token_urlsafe(18)
        now = time.monotonic()
        with self._lock:
            group = self._sessions.setdefault(workspace_id, {})
            global_active = sum(
                not session.is_closed
                for sessions in self._sessions.values()
                for session in sessions.values()
            )
            if global_active >= self._max_sessions:
                if not group:
                    self._sessions.pop(workspace_id, None)
                raise HTTPException(status_code=503, detail="broker terminal limit reached")
            active = sum(not session.is_closed for session in group.values())
            if active >= MAX_TERMINAL_SESSIONS_PER_WORKSPACE:
                raise HTTPException(status_code=409, detail="workspace terminal limit reached")
            spec = SandboxSpec(
                argv=["/bin/sh"],
                workdir=str(workdir),
                env={
                    **self._backend.base_env(),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "TERM": "xterm-256color",
                },
                phase="agent",
            )
            try:
                process = self._backend.spawn_terminal(spec, rows=rows, cols=cols)
            except TerminalNotSupportedError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=("Interactive terminals require the container or kata sandbox backend."),
                ) from exc
            except (OSError, SandboxError) as exc:
                raise HTTPException(
                    status_code=502, detail="terminal could not be started"
                ) from exc
            session = _TerminalSession(
                terminal_id=terminal_id,
                process=process,
                workdir=workdir,
                rows=rows,
                cols=cols,
                now=now,
            )
            group[terminal_id] = session
        session.start()
        return session.descriptor()

    def list(self, workspace_id: str) -> list[dict[str, Any]]:
        """List visible terminals for one workspace only."""
        self.reap()
        with self._lock:
            sessions = list(self._sessions.get(workspace_id, {}).values())
        return [session.descriptor() for session in sessions if not session.is_closed]

    def get(self, workspace_id: str, terminal_id: str) -> _TerminalSession:
        """Resolve an opaque id inside its workspace namespace."""
        self.reap()
        with self._lock:
            session = self._sessions.get(workspace_id, {}).get(terminal_id)
        if session is None or session.is_closed:
            raise HTTPException(status_code=404, detail="terminal does not exist")
        return session

    def delete(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Close a terminal, retaining it when cleanup needs to be retried."""
        with self._lock:
            group = self._sessions.get(workspace_id, {})
            session = group.get(terminal_id)
        if session is None:
            return {
                "id": terminal_id,
                "shell": "sh",
                "state": "closed",
                "cwd": "/workspace",
                "rows": 24,
                "cols": 80,
                "last_seq": 0,
            }
        try:
            descriptor = session.close()
        except (SandboxError, OSError) as exc:
            raise HTTPException(
                status_code=503,
                detail="terminal cleanup could not be confirmed; retry kill",
            ) from exc
        with self._lock:
            group = self._sessions.get(workspace_id)
            if group is not None and group.get(terminal_id) is session:
                group.pop(terminal_id, None)
                if not group:
                    self._sessions.pop(workspace_id, None)
        return descriptor

    def reap(self) -> None:
        """Close sessions that exceeded either configured lifetime."""
        now = time.monotonic()
        with self._lock:
            expired = [
                (workspace_id, terminal_id, session)
                for workspace_id, group in self._sessions.items()
                for terminal_id, session in group.items()
                if session.expired(
                    now=now,
                    idle_ttl_s=self._idle_ttl_s,
                    hard_ttl_s=self._hard_ttl_s,
                )
            ]
        for workspace_id, terminal_id, session in expired:
            try:
                session.close()
            except (SandboxError, OSError):
                logger.exception("workspace_terminal_reap_cleanup_failed")
                continue
            with self._lock:
                group = self._sessions.get(workspace_id)
                if group is not None and group.get(terminal_id) is session:
                    group.pop(terminal_id, None)
                    if not group:
                        self._sessions.pop(workspace_id, None)

    def _reap_loop(self) -> None:
        interval = max(0.1, min(30.0, self._idle_ttl_s / 2, self._hard_ttl_s / 2))
        while not self._stop.wait(interval):
            self.reap()


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


def _terminal_sse_frame(event: _TerminalEvent) -> str:
    """Serialize one terminal event using the gateway's stable SSE contract."""
    if event.kind == "output":
        payload = {
            "seq": event.seq,
            "data": base64.b64encode(event.data).decode("ascii"),
        }
    elif event.kind == "reset":
        payload = {"seq": event.seq, "reason": event.reason}
    else:
        payload = {"seq": event.seq, "exit_code": event.exit_code}
    data = json.dumps(payload, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


def create_app(
    *,
    backend: SandboxBackend | None = None,
    workdir_root: str | None = None,
    token: str | None = None,
    terminal_idle_ttl_s: float | None = None,
    terminal_hard_ttl_s: float | None = None,
    terminal_max_sessions: int | None = None,
) -> FastAPI:
    """Create the internal broker app with injectable dependencies for tests."""
    sandbox = backend or build_backend_from_env()
    root = workdir_root or os.environ.get(
        "AGENT_WORKDIR_ROOT", "/var/lib/hybridinference/agent-jobs"
    )
    expected_token = (
        token if token is not None else os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN", "")
    )
    idle_ttl_s = (
        terminal_idle_ttl_s
        if terminal_idle_ttl_s is not None
        else float(os.environ.get("AGENT_TERMINAL_IDLE_TTL_SECONDS", DEFAULT_TERMINAL_IDLE_TTL_S))
    )
    hard_ttl_s = (
        terminal_hard_ttl_s
        if terminal_hard_ttl_s is not None
        else float(os.environ.get("AGENT_TERMINAL_HARD_TTL_SECONDS", DEFAULT_TERMINAL_HARD_TTL_S))
    )
    max_sessions = (
        terminal_max_sessions
        if terminal_max_sessions is not None
        else int(os.environ.get("AGENT_TERMINAL_MAX_SESSIONS", DEFAULT_MAX_TERMINAL_SESSIONS))
    )
    if idle_ttl_s <= 0 or hard_ttl_s <= 0:
        raise ValueError("terminal TTL values must be positive")
    if not 1 <= max_sessions <= 4096:
        raise ValueError("terminal global session limit must be between 1 and 4096")
    terminals = _TerminalManager(
        sandbox,
        idle_ttl_s=idle_ttl_s,
        hard_ttl_s=hard_ttl_s,
        max_sessions=max_sessions,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Path(root).mkdir(parents=True, exist_ok=True)
        sandbox.preflight(workdir_root=root)
        # A workdir root has one active broker. Container backends use that
        # stable scope to remove only crash leftovers from a previous instance.
        sandbox.prepare_terminal_broker(root)
        terminals.start()
        try:
            yield
        finally:
            terminals.close()

    app = FastAPI(title="Agent workspace broker", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.terminal_manager = terminals

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

    @app.post("/workspaces/{workspace_id}/terminals")
    async def create_terminal(
        workspace_id: str,
        body: TerminalCreateRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        workdir = _workspace(root, workspace_id)
        return await asyncio.to_thread(
            terminals.create,
            workspace_id,
            workdir,
            rows=body.rows,
            cols=body.cols,
        )

    @app.get("/workspaces/{workspace_id}/terminals")
    async def list_terminals(
        workspace_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        _workspace(root, workspace_id)
        return {"terminals": await asyncio.to_thread(terminals.list, workspace_id)}

    @app.get("/workspaces/{workspace_id}/terminals/{terminal_id}/stream")
    async def stream_terminal(
        workspace_id: str,
        terminal_id: str,
        after: int = Query(0, ge=0),
        _: None = Depends(authenticate),
    ) -> StreamingResponse:
        _workspace(root, workspace_id)
        session = terminals.get(workspace_id, terminal_id)
        session.open_stream()

        async def event_stream():
            cursor = after
            try:
                while True:
                    events = await session.events_after_async(
                        cursor,
                        wait_s=TERMINAL_STREAM_WAIT_S,
                    )
                    for event in events:
                        cursor = event.seq
                        yield _terminal_sse_frame(event)
                    descriptor = session.descriptor()
                    if descriptor["state"] != "running" and cursor >= descriptor["last_seq"]:
                        return
                    if not events:
                        # Keep intermediaries from timing out a quiet interactive
                        # shell without inventing a sequenced client event.
                        yield ": keepalive\n\n"
            finally:
                session.close_stream()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/workspaces/{workspace_id}/terminals/{terminal_id}/input")
    async def terminal_input(
        workspace_id: str,
        terminal_id: str,
        body: TerminalInputRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        _workspace(root, workspace_id)
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="terminal input is not valid base64"
            ) from exc
        if len(data) > MAX_TERMINAL_INPUT_BYTES:
            raise HTTPException(status_code=413, detail="terminal input is too large")
        session = terminals.get(workspace_id, terminal_id)
        return await asyncio.to_thread(session.write, data, now=time.monotonic())

    @app.post("/workspaces/{workspace_id}/terminals/{terminal_id}/resize")
    async def resize_terminal(
        workspace_id: str,
        terminal_id: str,
        body: TerminalResizeRequest,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        _workspace(root, workspace_id)
        session = terminals.get(workspace_id, terminal_id)
        return await asyncio.to_thread(
            session.resize,
            rows=body.rows,
            cols=body.cols,
            now=time.monotonic(),
        )

    @app.delete("/workspaces/{workspace_id}/terminals/{terminal_id}")
    async def delete_terminal(
        workspace_id: str,
        terminal_id: str,
        _: None = Depends(authenticate),
    ) -> dict[str, Any]:
        _workspace(root, workspace_id)
        return await asyncio.to_thread(terminals.delete, workspace_id, terminal_id)

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
    "TerminalCreateRequest",
    "TerminalInputRequest",
    "TerminalRequest",
    "TerminalResizeRequest",
    "app",
    "create_app",
]
