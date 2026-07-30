"""Regression tests for durable, owner-editable agent workspaces."""

from __future__ import annotations

import os
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from serving.agent_jobs.sandbox import ProcessBackend
from serving.agent_jobs.workspace_broker import create_app
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
