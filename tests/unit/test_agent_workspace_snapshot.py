"""Tests for the bounded changed-files workspace artifact."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from serving.agent_jobs import runner as runner_mod, workspace_snapshot as snapshot_mod
from serving.agent_jobs.runner import LeaseLost, build_patch, save_workspace_snapshot
from serving.agent_jobs.workspace_snapshot import build_workspace_snapshot


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "modified.txt").write_text("before\n")
    (repo / "deleted.txt").write_text("gone soon\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _entry(snapshot: dict, path: str) -> dict:
    return next(item for item in snapshot["files"] if item["path"] == path)


def test_snapshot_captures_added_modified_and_deleted_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "modified.txt").write_text("after\n")
    (repo / "deleted.txt").unlink()
    (repo / "added.txt").write_text("new\n")

    snapshot = json.loads(build_workspace_snapshot(str(repo), build_patch(str(repo))))

    assert snapshot["version"] == 1
    assert _entry(snapshot, "added.txt") == {
        "path": "added.txt",
        "status": "added",
        "size": 4,
        "content": "new\n",
    }
    assert _entry(snapshot, "modified.txt")["content"] == "after\n"
    assert _entry(snapshot, "modified.txt")["status"] == "modified"
    assert _entry(snapshot, "deleted.txt") == {
        "path": "deleted.txt",
        "status": "deleted",
        "size": 0,
    }


def test_snapshot_never_follows_final_or_intermediate_symlinks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not leak")
    (repo / "final-link").symlink_to(outside / "secret.txt")
    (repo / "directory-link").symlink_to(outside, target_is_directory=True)
    patch = """diff --git a/final-link b/final-link
new file mode 120000
--- /dev/null
+++ b/final-link
diff --git a/directory-link/secret.txt b/directory-link/secret.txt
--- a/directory-link/secret.txt
+++ b/directory-link/secret.txt
"""

    raw = build_workspace_snapshot(str(repo), patch)
    snapshot = json.loads(raw)

    assert _entry(snapshot, "final-link")["omitted"] == "symlink"
    assert _entry(snapshot, "final-link")["size"] == (repo / "final-link").lstat().st_size
    assert _entry(snapshot, "directory-link/secret.txt")["omitted"] == "symlink"
    assert "do not leak" not in raw


def test_snapshot_skips_git_and_escaping_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    patch = """diff --git a/.GiT/config b/.GiT/config
--- a/.GiT/config
+++ b/.GiT/config
diff --git a/../outside.txt b/../outside.txt
--- a/../outside.txt
+++ b/../outside.txt
"""

    snapshot = json.loads(build_workspace_snapshot(str(repo), patch))

    assert snapshot["files"] == []
    assert snapshot["truncated"] is True
    assert snapshot["skipped"] == 2


@pytest.mark.parametrize(
    "path",
    (
        "",
        "/etc/passwd",
        "../outside",
        "a/../outside",
        r"a\outside",
        "C:/outside",
        ".GIT/x",
        "a\x00b",
    ),
)
def test_unsafe_path_shapes_are_rejected(path):
    with pytest.raises(ValueError):
        snapshot_mod._path_parts(path)


def test_snapshot_marks_large_binary_and_total_limited_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "large.txt").write_text("12345")
    (repo / "binary.dat").write_bytes(b"a\x00b")
    (repo / "first.txt").write_text("1234")
    (repo / "second.txt").write_text("5678")
    patch = "".join(
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        for path in ("large.txt", "binary.dat", "first.txt", "second.txt")
    )

    snapshot = json.loads(
        build_workspace_snapshot(str(repo), patch, max_file_bytes=4, max_total_bytes=7)
    )

    assert _entry(snapshot, "large.txt")["omitted"] == "too_large"
    assert _entry(snapshot, "binary.dat")["omitted"] == "binary"
    included = [
        entry
        for entry in snapshot["files"]
        if entry["path"] in {"first.txt", "second.txt"} and "content" in entry
    ]
    limited = [
        entry
        for entry in snapshot["files"]
        if entry["path"] in {"first.txt", "second.txt"} and "content" not in entry
    ]
    assert len(included) == 1
    assert limited[0]["omitted"] == "total_limit"
    assert snapshot["truncated"] is True


def test_snapshot_does_not_exceed_total_read_limit_if_a_file_grows(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "growing.txt").write_text("0123456789")
    patch = """diff --git a/growing.txt b/growing.txt
new file mode 100644
--- /dev/null
+++ b/growing.txt
"""
    real_fstat = snapshot_mod.os.fstat
    fstat_calls = 0

    def changing_fstat(fd):
        nonlocal fstat_calls
        info = real_fstat(fd)
        fstat_calls += 1
        if fstat_calls == 1:
            return SimpleNamespace(st_mode=info.st_mode, st_size=2)
        return info

    real_read = snapshot_mod.os.read
    bytes_read = 0

    def tracked_read(fd, count):
        nonlocal bytes_read
        data = real_read(fd, count)
        bytes_read += len(data)
        return data

    monkeypatch.setattr(snapshot_mod.os, "fstat", changing_fstat)
    monkeypatch.setattr(snapshot_mod.os, "read", tracked_read)

    snapshot = json.loads(build_workspace_snapshot(str(repo), patch, max_total_bytes=4))

    assert bytes_read == 4
    assert _entry(snapshot, "growing.txt")["omitted"] == "total_limit"


def test_snapshot_file_count_is_bounded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.txt", "b.txt"):
        (repo / name).write_text(name)
    patch = "".join(
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        for path in ("a.txt", "b.txt")
    )

    snapshot = json.loads(build_workspace_snapshot(str(repo), patch, max_files=1))

    assert len(snapshot["files"]) == 1
    assert snapshot["truncated"] is True


def test_serialized_snapshot_is_bounded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "generated.txt").write_text("x" * 500)
    patch = """diff --git a/generated.txt b/generated.txt
new file mode 100644
--- /dev/null
+++ b/generated.txt
"""

    raw = build_workspace_snapshot(
        str(repo),
        patch,
        max_file_bytes=1000,
        max_total_bytes=1000,
        max_artifact_bytes=400,
    )
    snapshot = json.loads(raw)

    assert len(raw.encode()) <= 400
    assert _entry(snapshot, "generated.txt")["omitted"] == "artifact_limit"
    assert snapshot["truncated"] is True


class _FailingControl:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def save_artifact(self, kind: str, content: str) -> None:
        assert kind == "workspace_snapshot"
        assert json.loads(content)["version"] == 1
        raise self.error


def test_snapshot_upload_failure_does_not_break_terminal_handling(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "build_workspace_snapshot", lambda *_args: '{"version":1}')

    stored = save_workspace_snapshot(
        _FailingControl(RuntimeError("store unavailable")), workdir=str(tmp_path), patch=""
    )

    assert stored is False


def test_snapshot_upload_still_honours_lease_fencing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "build_workspace_snapshot", lambda *_args: '{"version":1}')

    with pytest.raises(LeaseLost):
        save_workspace_snapshot(
            _FailingControl(LeaseLost("superseded")), workdir=str(tmp_path), patch=""
        )


def test_runner_stores_snapshot_before_the_terminal_transition(tmp_path, monkeypatch):
    order: list[str] = []

    class Control:
        job_id = ""

        def append_event(self, _event) -> None:
            pass

        def save_artifact(self, kind: str, _content: str) -> None:
            order.append(f"artifact:{kind}")

        def finish(self, _state: str, _detail=None, *, base_sha=None) -> None:
            order.append("finish")

        def close(self) -> None:
            pass

    class Backend:
        name = "test"
        provides_isolation = False

        def preflight(self) -> None:
            pass

        def has_binary(self, _binary: str) -> bool:
            return True

        def adopt_workdir(self, _workdir: str) -> None:
            pass

        def sandbox_metadata(self) -> dict[str, str]:
            return {"sandbox_backend": self.name}

    control = Control()
    job = runner_mod.ClaimedJob(
        job_id="ajob_1",
        attempt_id=1,
        attempt_no=1,
        repo="owner/repo",
        base_sha="abcdef0",
        task_prompt="task",
        runtime="generic",
        model="model",
        worker_token="worker-token",
        sandbox_token="model-token",
    )
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: job)
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: control)
    monkeypatch.setattr(
        runner_mod, "get_runtime", lambda *_args, **_kwargs: SimpleNamespace(binary="x")
    )
    monkeypatch.setattr(runner_mod, "existing_checkout_sha", lambda *_args: "abcdef0")
    monkeypatch.setattr(runner_mod, "align_existing_checkout", lambda *_args, **_kwargs: "abcdef0")
    monkeypatch.setattr(runner_mod, "run_agent", lambda *_args, **_kwargs: (0, "", []))
    monkeypatch.setattr(runner_mod, "build_patch", lambda *_args, **_kwargs: "diff --git a/x b/x\n")

    def snapshot(_control, *, workdir: str, patch: str) -> bool:
        assert workdir == str(tmp_path)
        assert patch.startswith("diff --git")
        order.append("artifact:workspace_snapshot")
        return True

    monkeypatch.setattr(runner_mod, "save_workspace_snapshot", snapshot)

    result = runner_mod.run_once(
        base_url="http://gateway",
        dispatcher_token="dispatcher",
        worker_id="worker",
        workdir=str(tmp_path),
        backend=Backend(),
    )

    assert result == 0
    assert order == ["artifact:patch", "artifact:workspace_snapshot", "finish"]
