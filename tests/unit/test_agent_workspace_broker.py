"""Regression tests for durable, owner-editable agent workspaces."""

from __future__ import annotations

import asyncio
import base64
import os
import queue
import subprocess
import threading

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from serving.agent_jobs.sandbox import (
    ProcessBackend,
    SandboxBackend,
    SandboxError,
    SandboxSpec,
    TerminalProcess,
)
from serving.agent_jobs.workspace_broker import (
    MAX_TERMINAL_BUFFER_BYTES,
    MAX_TERMINAL_BUFFER_EVENTS,
    _terminal_sse_frame,
    _TerminalSession,
    create_app,
)
from serving.agent_jobs.workspace_paths import (
    WorkspaceIdError,
    purge_stale_workspaces,
    workspace_path,
)


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _workspace(tmp_path):
    root = tmp_path / "workspaces"
    repo = root / "ajob_test"
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "agent@example.test")
    _git(repo, "config", "user.name", "Agent")
    (repo / "README.md").write_text("before\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "base")
    return root, repo


class _FakeTerminalProcess(TerminalProcess):
    """Deterministic byte terminal for broker protocol tests."""

    def __init__(self) -> None:
        self.inputs: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.killed = False
        self.suspended = False
        self.suspensions: list[bool] = []
        self._natural_exit = False
        self._output: queue.Queue[bytes | None] = queue.Queue()
        self._output.put(b"prompt> ")
        self._lock = threading.Lock()

    def chunks(self):
        while (chunk := self._output.get()) is not None:
            yield chunk

    def write(self, data: bytes) -> None:
        self.inputs.append(data)
        if data == b"exit\n":
            self._output.put(b"bye\r\n")
            self._natural_exit = True
            self._output.put(None)

    def resize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def suspend(self) -> None:
        self.suspended = True
        self.suspensions.append(True)

    def resume(self) -> None:
        self.suspended = False
        self.suspensions.append(False)

    def kill(self) -> None:
        with self._lock:
            if self.killed or self._natural_exit:
                return
            self.killed = True
            self._output.put(None)

    def wait(self) -> int:
        return 137 if self.killed else 0


class _FakeTerminalBackend(SandboxBackend):
    """Backend that records PTY operations without starting host processes."""

    def __init__(self) -> None:
        self.processes: list[_FakeTerminalProcess] = []
        self.specs: list[SandboxSpec] = []

    def spawn(self, spec: SandboxSpec):
        raise AssertionError("the legacy command path is not used by this fixture")

    def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
        process = _FakeTerminalProcess()
        process.sizes.append((rows, cols))
        self.specs.append(spec)
        self.processes.append(process)
        return process


class _RecordingTerminalBackend(_FakeTerminalBackend):
    """Record broker-only startup preparation without touching Docker."""

    def __init__(self) -> None:
        super().__init__()
        self.prepared_roots: list[str] = []

    def prepare_terminal_broker(self, workdir_root: str) -> None:
        self.prepared_roots.append(workdir_root)


async def _wait_for_last_seq(
    client: AsyncClient,
    headers: dict[str, str],
    terminal_id: str,
    expected: int,
) -> None:
    """Wait for the background output pump without timing-based assertions."""
    for _ in range(100):
        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        descriptor = next(
            terminal for terminal in listing.json()["terminals"] if terminal["id"] == terminal_id
        )
        if descriptor["last_seq"] >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"terminal did not reach sequence {expected}")


def test_workspace_path_rejects_directory_escape(tmp_path):
    with pytest.raises(WorkspaceIdError):
        workspace_path(tmp_path, "../other")


def test_workspace_ttl_removes_only_expired_real_directories(tmp_path):
    root = tmp_path / "workspaces"
    stale = root / "athr_stale"
    fresh = root / "athr_fresh"
    stale.mkdir(parents=True)
    fresh.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "athr_link")
    os.utime(stale, (10, 10))
    os.utime(fresh, (90, 90))

    removed = purge_stale_workspaces(root, ttl_seconds=50, now=100)

    assert removed == ["athr_stale"]
    assert not stale.exists()
    assert fresh.exists()
    assert outside.exists(), "cleanup must not follow a workspace symlink"


@pytest.mark.asyncio
async def test_broker_browses_and_writes_the_real_worktree(tmp_path):
    root, repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        listing = await client.get("/workspaces/ajob_test/files", headers=headers)
        saved = await client.put(
            "/workspaces/ajob_test/files",
            params={"path": "README.md"},
            json={"content": "after\n"},
            headers=headers,
        )

    assert listing.status_code == 200
    assert listing.json()["source"] == "workspace"
    assert listing.json()["writable"] is True
    assert saved.status_code == 200
    assert saved.json()["content"] == "after\n"
    assert repo.joinpath("README.md").read_text() == "after\n"


@pytest.mark.asyncio
async def test_broker_refuses_auth_bypass_and_symlink_writes(tmp_path):
    root, repo = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, repo / "escape")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "private.txt").write_text("private")
    os.symlink(outside_dir, repo / "escape-dir")
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        unauthenticated = await client.get("/workspaces/ajob_test/files")
        write = await client.put(
            "/workspaces/ajob_test/files",
            params={"path": "escape"},
            json={"content": "stolen"},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )
        symlink = await client.get(
            "/workspaces/ajob_test/files",
            params={"path": "escape"},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )
        nested = await client.get(
            "/workspaces/ajob_test/files",
            params={"path": "escape-dir/private.txt"},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )

    assert unauthenticated.status_code == 401
    assert write.status_code == 409
    assert symlink.status_code == 200
    assert symlink.json()["kind"] == "symlink"
    assert "secret" not in str(symlink.json())
    assert nested.status_code == 400
    assert outside.read_text() == "secret"


@pytest.mark.asyncio
async def test_broker_terminal_and_git_operate_on_the_same_worktree(tmp_path):
    root, repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        terminal = await client.post(
            "/workspaces/ajob_test/terminal",
            json={"command": "printf terminal > terminal.txt", "cwd": str(repo)},
            headers=headers,
        )
        git = await client.get("/workspaces/ajob_test/git", headers=headers)

    assert terminal.status_code == 200
    assert terminal.json()["exit_code"] == 0
    assert repo.joinpath("terminal.txt").read_text() == "terminal"
    assert git.status_code == 200
    assert git.json()["available"] is True
    assert any(change["path"] == "terminal.txt" for change in git.json()["changes"])
    assert "terminal.txt" in git.json()["patch"]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert status == "?? terminal.txt\n", "Git inspection must not mutate the real index"


@pytest.mark.asyncio
async def test_broker_git_keeps_local_commits_in_the_base_diff(tmp_path):
    root, repo = _workspace(tmp_path)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "committed.txt").write_text("from terminal\n")
    _git(repo, "add", "committed.txt")
    _git(repo, "commit", "--quiet", "-m", "terminal commit")
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        response = await client.get(
            "/workspaces/ajob_test/git",
            params={"base_sha": base_sha},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )

    assert response.status_code == 200
    assert "committed.txt" in response.json()["patch"]


@pytest.mark.asyncio
async def test_broker_terminal_session_create_list_input_resize_and_resume_stream(tmp_path):
    """The private protocol carries raw PTY bytes with resumable sequence ids."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/ajob_test/terminals",
            json={"rows": 30, "cols": 120},
            headers=headers,
        )
        assert created.status_code == 200
        terminal = created.json()
        terminal_id = terminal["id"]
        assert terminal == {
            "id": terminal_id,
            "shell": "sh",
            "state": "running",
            "cwd": "/workspace",
            "rows": 30,
            "cols": 120,
            "last_seq": terminal["last_seq"],
        }

        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["terminals"][0]["id"] == terminal_id

        resized = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/resize",
            json={"rows": 42, "cols": 132},
            headers=headers,
        )
        sent = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": base64.b64encode(b"exit\n").decode()},
            headers=headers,
        )
        assert resized.status_code == 200
        assert resized.json()["rows"] == 42
        assert resized.json()["cols"] == 132
        assert sent.status_code == 200
        assert backend.processes[0].inputs == [b"exit\n"]
        assert backend.processes[0].sizes == [(30, 120), (42, 132)]

        await _wait_for_last_seq(client, headers, terminal_id, 3)
        stream = await client.get(
            f"/workspaces/ajob_test/terminals/{terminal_id}/stream",
            params={"after": 1},
            headers=headers,
        )

    assert stream.status_code == 200
    assert "id: 1\n" not in stream.text
    assert 'id: 2\nevent: output\ndata: {"seq":2,"data":"YnllDQo="}\n\n' in stream.text
    assert 'id: 3\nevent: exit\ndata: {"seq":3,"exit_code":0}\n\n' in stream.text


@pytest.mark.asyncio
async def test_broker_suspends_and_resumes_terminal_process_trees(tmp_path):
    """Protected workspace phases freeze retained PTYs without closing them."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers=headers,
        )
        terminal_id = created.json()["id"]
        suspended = await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 1},
            headers=headers,
        )
        blocked_input = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        blocked_create = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers=headers,
        )
        resumed = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 1},
            headers=headers,
        )
        accepted_input = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        suspended_by_retry = await client.post(
            "/workspaces/ajob_test/terminals/suspend",
            json={"lease_generation": 2},
            headers=headers,
        )
        stale_resume = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 1},
            headers=headers,
        )
        current_resume = await client.post(
            "/workspaces/ajob_test/terminals/resume",
            json={"lease_generation": 2},
            headers=headers,
        )
        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)

    assert suspended.json() == {"ok": True, "suspended": 1}
    assert blocked_input.status_code == 409
    assert blocked_create.status_code == 409
    assert resumed.json() == {"ok": True, "resumed": 1}
    assert accepted_input.status_code == 200
    assert suspended_by_retry.status_code == 200
    assert stale_resume.status_code == 409
    assert current_resume.status_code == 200
    assert backend.processes[0].suspensions == [True, False, True, False]
    assert backend.processes[0].inputs == [b"x"]
    assert listing.json()["terminals"][0]["id"] == terminal_id


@pytest.mark.asyncio
async def test_broker_terminal_kill_is_idempotent_and_workspace_bound(tmp_path):
    """An opaque id cannot operate another workspace and delete never leaks it."""
    root, _repo = _workspace(tmp_path)
    other = root / "ajob_other"
    other.mkdir()
    _git(other, "init", "--quiet")
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers=headers,
        )
        terminal_id = created.json()["id"]
        cross_workspace = await client.post(
            f"/workspaces/ajob_other/terminals/{terminal_id}/input",
            json={"data": "eA=="},
            headers=headers,
        )
        cross_delete = await client.delete(
            f"/workspaces/ajob_other/terminals/{terminal_id}", headers=headers
        )
        still_live = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        killed = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )
        killed_again = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )
        listing = await client.get("/workspaces/ajob_test/terminals", headers=headers)

    assert cross_workspace.status_code == 404
    assert cross_delete.status_code == 200
    assert cross_delete.json()["state"] == "closed"
    assert still_live.json()["terminals"][0]["id"] == terminal_id
    assert killed.status_code == 200
    assert killed.json()["state"] == "closed"
    assert backend.processes[0].killed is True
    assert killed_again.status_code == 200
    assert killed_again.json()["state"] == "closed"
    assert killed_again.json()["last_seq"] == 0
    assert listing.json() == {"terminals": []}


@pytest.mark.asyncio
async def test_broker_retains_terminal_when_kill_cleanup_needs_retry(tmp_path):
    """A transient cleanup failure keeps the opaque id available for retry."""

    class _RetryProcess(_FakeTerminalProcess):
        def __init__(self) -> None:
            super().__init__()
            self.kill_attempts = 0

        def kill(self) -> None:
            self.kill_attempts += 1
            if self.kill_attempts == 1:
                raise SandboxError("daemon unavailable")
            super().kill()

    class _RetryBackend(_FakeTerminalBackend):
        def spawn_terminal(self, spec: SandboxSpec, *, rows: int, cols: int) -> TerminalProcess:
            process = _RetryProcess()
            process.sizes.append((rows, cols))
            self.specs.append(spec)
            self.processes.append(process)
            return process

    root, _repo = _workspace(tmp_path)
    backend = _RetryBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        created = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        terminal_id = created.json()["id"]
        failed = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )
        retained = app.state.terminal_manager._sessions["ajob_test"][terminal_id]
        retained_state = retained.descriptor()["state"]
        retried = await client.delete(
            f"/workspaces/ajob_test/terminals/{terminal_id}", headers=headers
        )

    assert failed.status_code == 503
    assert "retry kill" in failed.json()["detail"]
    assert retained_state == "cleanup_failed"
    assert backend.processes[0].kill_attempts == 2
    assert retried.status_code == 200
    assert retried.json()["state"] == "closed"
    assert app.state.terminal_manager._sessions == {}


def test_terminal_output_limit_stops_a_runaway_process(monkeypatch, tmp_path):
    """A noisy command cannot stream unbounded bytes through the trusted broker."""
    monkeypatch.setattr("serving.agent_jobs.workspace_broker.MAX_TERMINAL_TOTAL_OUTPUT_BYTES", 10)
    process = _FakeTerminalProcess()
    process._output.put(b"overflow")
    session = _TerminalSession(
        terminal_id="term_test",
        process=process,
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )

    session.start()
    session._pump_thread.join(timeout=2)
    events = session.events_after(0, wait_s=0)

    assert process.killed is True
    assert session.descriptor()["state"] == "exited"
    assert any(b"output limit exceeded" in event.data for event in events)
    assert events[-1].kind == "exit"


def test_terminal_stream_viewers_are_bounded_per_session(tmp_path):
    """Duplicate SSE viewers cannot create unbounded broker waiters."""
    process = _FakeTerminalProcess()
    session = _TerminalSession(
        terminal_id="term_test",
        process=process,
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )
    session.start()

    for _ in range(4):
        session.open_stream()
    with pytest.raises(HTTPException) as excinfo:
        session.open_stream()
    session.close_stream()
    session.open_stream()

    assert excinfo.value.status_code == 429
    session.close()


@pytest.mark.asyncio
async def test_terminal_output_wakes_every_async_viewer(tmp_path):
    """One viewer cannot clear another viewer's output notification."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )
    first = asyncio.create_task(session.events_after_async(0, wait_s=1))
    second = asyncio.create_task(session.events_after_async(0, wait_s=1))
    await asyncio.sleep(0)

    session._append_output(b"ready")
    first_events, second_events = await asyncio.gather(first, second)

    assert [event.data for event in first_events] == [b"ready"]
    assert [event.data for event in second_events] == [b"ready"]


@pytest.mark.asyncio
async def test_broker_terminal_auth_validation_input_bound_and_session_limit(tmp_path):
    """Broker auth and all request/resource bounds fail before unsafe work."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        unauthenticated = await client.post(
            "/workspaces/ajob_test/terminals", json={"rows": 24, "cols": 80}
        )
        bad_rows = await client.post(
            "/workspaces/ajob_test/terminals",
            json={"rows": 1, "cols": 80},
            headers=headers,
        )
        bad_cols = await client.post(
            "/workspaces/ajob_test/terminals",
            json={"rows": 24, "cols": 501},
            headers=headers,
        )
        created = [
            await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
            for _ in range(4)
        ]
        over_limit = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        terminal_id = created[0].json()["id"]
        invalid_base64 = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": "not base64!"},
            headers=headers,
        )
        too_large = await client.post(
            f"/workspaces/ajob_test/terminals/{terminal_id}/input",
            json={"data": base64.b64encode(b"x" * (64 * 1024 + 1)).decode()},
            headers=headers,
        )
        negative_resume = await client.get(
            f"/workspaces/ajob_test/terminals/{terminal_id}/stream",
            params={"after": -1},
            headers=headers,
        )
        for response in created:
            await client.delete(
                f"/workspaces/ajob_test/terminals/{response.json()['id']}",
                headers=headers,
            )

    assert unauthenticated.status_code == 401
    assert bad_rows.status_code == 422
    assert bad_cols.status_code == 422
    assert all(response.status_code == 200 for response in created)
    assert over_limit.status_code == 409
    assert invalid_base64.status_code == 400
    assert too_large.status_code == 413
    assert negative_resume.status_code == 422


@pytest.mark.asyncio
async def test_broker_terminal_global_limit_bounds_all_workspaces(tmp_path):
    """Many jobs cannot collectively exhaust the broker host with PTYs."""
    root, _repo = _workspace(tmp_path)
    other = root / "ajob_other"
    other.mkdir()
    _git(other, "init", "--quiet")
    backend = _FakeTerminalBackend()
    app = create_app(
        backend=backend,
        workdir_root=str(root),
        token="broker-secret",
        terminal_max_sessions=2,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        first = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        second = await client.post("/workspaces/ajob_other/terminals", json={}, headers=headers)
        rejected = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await client.delete(
            f"/workspaces/ajob_test/terminals/{first.json()['id']}", headers=headers
        )
        replacement = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await client.delete(
            f"/workspaces/ajob_other/terminals/{second.json()['id']}", headers=headers
        )
        await client.delete(
            f"/workspaces/ajob_test/terminals/{replacement.json()['id']}", headers=headers
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert rejected.status_code == 503
    assert replacement.status_code == 200


@pytest.mark.asyncio
async def test_broker_terminal_idle_ttl_and_shutdown_cleanup_kill_processes(tmp_path):
    """Forgotten sessions are killed on TTL and broker shutdown."""
    root, _repo = _workspace(tmp_path)
    backend = _FakeTerminalBackend()
    app = create_app(
        backend=backend,
        workdir_root=str(root),
        token="broker-secret",
        terminal_idle_ttl_s=0.1,
        terminal_hard_ttl_s=60,
    )
    headers = {"X-Agent-Workspace-Token": "broker-secret"}
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        first = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)
        await _wait_for_last_seq(client, headers, first.json()["id"], 1)
        await asyncio.sleep(0.12)
        expired = await client.get("/workspaces/ajob_test/terminals", headers=headers)
        second = await client.post("/workspaces/ajob_test/terminals", json={}, headers=headers)

    assert expired.json() == {"terminals": []}
    assert backend.processes[0].killed is True
    assert second.status_code == 200
    app.state.terminal_manager.close()
    assert backend.processes[1].killed is True


@pytest.mark.asyncio
async def test_broker_lifespan_runs_terminal_cleanup_only_for_its_workdir_root(tmp_path):
    """Broker startup, rather than ordinary runner preflight, owns crash cleanup."""
    root, _repo = _workspace(tmp_path)
    backend = _RecordingTerminalBackend()
    app = create_app(backend=backend, workdir_root=str(root), token="broker-secret")

    async with app.router.lifespan_context(app):
        assert backend.prepared_roots == [str(root)]


@pytest.mark.asyncio
async def test_broker_explains_that_process_backend_cannot_host_persistent_terminals(tmp_path):
    """Unsafe local PTYs are rejected with an actionable owner-facing response."""
    root, _repo = _workspace(tmp_path)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        workdir_root=str(root),
        token="broker-secret",
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://broker") as client:
        response = await client.post(
            "/workspaces/ajob_test/terminals",
            json={},
            headers={"X-Agent-Workspace-Token": "broker-secret"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Interactive terminals require the container or kata sandbox backend."
    }


def test_terminal_ring_limits_many_one_byte_events_and_signals_cursor_reset(tmp_path):
    """An event-count eviction is explicit and the retained cursor remains resumable."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )

    for _ in range(MAX_TERMINAL_BUFFER_EVENTS + 1):
        session._append_output(b"x")

    events = session.events_after(0, wait_s=0)
    assert len(events) == MAX_TERMINAL_BUFFER_EVENTS
    assert events[0].kind == "reset"
    assert events[0].reason == "output_truncated"
    assert events[1].kind == "output"
    assert events[1].seq == events[0].seq + 1
    assert events[-1].seq == MAX_TERMINAL_BUFFER_EVENTS + 1
    assert _terminal_sse_frame(events[0]) == (
        f"id: {events[0].seq}\nevent: reset\n"
        f'data: {{"seq":{events[0].seq},"reason":"output_truncated"}}\n\n'
    )

    resumed = session.events_after(events[0].seq, wait_s=0)
    assert resumed[0].kind == "output"
    assert all(event.kind != "reset" for event in resumed)


def test_terminal_ring_marks_a_single_oversized_output_before_its_retained_tail(tmp_path):
    """Trimming inside one backend chunk also emits a separately resumable reset."""
    session = _TerminalSession(
        terminal_id="term_test",
        process=_FakeTerminalProcess(),
        workdir=tmp_path,
        rows=24,
        cols=80,
        now=0,
    )

    session._append_output(b"a" + b"b" * MAX_TERMINAL_BUFFER_BYTES)

    events = session.events_after(0, wait_s=0)
    assert [(event.seq, event.kind) for event in events] == [(1, "reset"), (2, "output")]
    assert events[0].reason == "output_truncated"
    assert events[1].data == b"b" * MAX_TERMINAL_BUFFER_BYTES
    assert session.events_after(1, wait_s=0) == [events[1]]
