"""The setup phase and its filesystem snapshot cache.

Installing dependencies is the slow part of a job and the only part that needs
the network, which is why the design separates it: its own egress tier, and a
cached result so a retry does not reinstall.
"""

from __future__ import annotations

import os
import tarfile
import time
from typing import TYPE_CHECKING

import pytest

from serving.agent_jobs.setup import SnapshotCache, cache_key, run_setup

if TYPE_CHECKING:
    from pathlib import Path


def test_the_key_follows_the_script_not_the_commit():
    """Dependencies change with the manifest, not with every commit.

    Keying on the sha would miss nearly every hit and make the cache pointless;
    keying on the script means the entry is invalidated exactly when the thing
    it captured would differ.
    """
    first = cache_key("o/n", "pip install -r requirements.txt")
    again = cache_key("o/n", "pip install -r requirements.txt")
    changed = cache_key("o/n", "pip install -r requirements.txt --upgrade")
    other_repo = cache_key("other/n", "pip install -r requirements.txt")

    assert first == again
    assert first != changed
    assert first != other_repo, "a snapshot must never be shared across repositories"


def test_a_snapshot_round_trips(tmp_path: Path):
    """What setup produced is what a later job gets back."""
    work = tmp_path / "job1"
    (work / "node_modules" / "pkg").mkdir(parents=True)
    (work / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n")
    cache = SnapshotCache(str(tmp_path / "cache"))

    assert cache.save("k", str(work)) is True

    restored = tmp_path / "job2"
    restored.mkdir()
    assert cache.restore("k", str(restored)) is True
    assert (restored / "node_modules" / "pkg" / "index.js").read_text() == "module.exports = 1;\n"


def test_the_checkout_is_never_captured(tmp_path: Path):
    """`.git` is the job's own checkout, and carries credential-bearing files.

    A later job checks out its own commit, so capturing this would be both
    wrong and a way to carry one job's git state into another's worktree.
    """
    work = tmp_path / "job1"
    (work / ".git").mkdir(parents=True)
    (work / ".git" / "config").write_text("[remote]\n")
    (work / "vendor").mkdir()
    (work / "vendor" / "lib.py").write_text("x = 1\n")
    cache = SnapshotCache(str(tmp_path / "cache"))
    cache.save("k", str(work))

    restored = tmp_path / "job2"
    restored.mkdir()
    cache.restore("k", str(restored))

    assert (restored / "vendor" / "lib.py").exists()
    assert not (restored / ".git").exists()


def test_an_expired_snapshot_is_dropped_not_served(tmp_path: Path):
    """A stale entry means a wrong dependency tree, so it expires."""
    work = tmp_path / "job1"
    work.mkdir()
    (work / "dep.txt").write_text("old\n")
    cache = SnapshotCache(str(tmp_path / "cache"), ttl_seconds=1)
    cache.save("k", str(work))

    stored = tmp_path / "cache" / "k.tar"
    os.utime(stored, (time.time() - 10, time.time() - 10))

    restored = tmp_path / "job2"
    restored.mkdir()
    assert cache.restore("k", str(restored)) is False
    # And it is removed, not merely ignored: a cache that only grows is a
    # problem on a host that also holds every job's worktree.
    assert not stored.exists()


def test_an_archive_cannot_write_outside_the_worktree(tmp_path: Path):
    """The snapshot came out of running a repository's own setup script.

    So it is treated as untrusted input like everything else that tree
    touched: a member that escapes the worktree stops the restore.
    """
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    hostile = cache_root / "k.tar"
    victim = tmp_path / "outside.txt"
    victim.write_text("original\n")
    payload = tmp_path / "payload.txt"
    payload.write_text("overwritten\n")
    with tarfile.open(hostile, "w") as archive:
        archive.add(payload, arcname="../outside.txt")

    work = tmp_path / "job"
    work.mkdir()
    assert SnapshotCache(str(cache_root)).restore("k", str(work)) is False
    assert victim.read_text() == "original\n"


def test_purging_removes_only_what_expired(tmp_path: Path):
    """Housekeeping must not throw away live entries."""
    work = tmp_path / "job"
    work.mkdir()
    (work / "f").write_text("x")
    cache = SnapshotCache(str(tmp_path / "cache"), ttl_seconds=60)
    cache.save("fresh", str(work))
    cache.save("stale", str(work))
    stale = tmp_path / "cache" / "stale.tar"
    os.utime(stale, (time.time() - 3600, time.time() - 3600))

    assert cache.purge_expired() == 1
    assert (tmp_path / "cache" / "fresh.tar").exists()
    assert not stale.exists()


class _FakeBackend:
    """Records the spec it was asked to spawn."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.specs: list = []

    def spawn(self, spec):
        self.specs.append(spec)
        backend = self

        class _Process:
            def lines(self):
                return iter(["installing…\n"])

            def wait(self):
                return backend.exit_code

            def kill(self):
                return None

        return _Process()


def test_setup_runs_under_the_setup_phase(tmp_path: Path):
    """The phase is what selects the egress tier — setup may reach a registry.

    Spawning it as the agent phase would either deny the install or, worse,
    hand the agent turn the network the install needed.
    """
    work = tmp_path / "job"
    work.mkdir()
    backend = _FakeBackend()

    result = run_setup(
        script="pip install -r requirements.txt",
        workdir=str(work),
        repo="o/n",
        backend=backend,
        cache=None,
    )

    assert result.ran and not result.restored_from_cache
    assert backend.specs[0].phase == "setup"


def test_a_cache_hit_skips_the_install_entirely(tmp_path: Path):
    """The design says a resume must not re-run setup."""
    work = tmp_path / "job1"
    work.mkdir()
    (work / "vendor.txt").write_text("installed\n")
    cache = SnapshotCache(str(tmp_path / "cache"))
    cache.save(cache_key("o/n", "install"), str(work))

    second = tmp_path / "job2"
    second.mkdir()
    backend = _FakeBackend()
    result = run_setup(
        script="install", workdir=str(second), repo="o/n", backend=backend, cache=cache
    )

    assert result.restored_from_cache is True
    assert backend.specs == [], "a cache hit must not spawn anything"
    assert (second / "vendor.txt").read_text() == "installed\n"


def test_a_failed_setup_is_reported_and_not_cached(tmp_path: Path):
    """Caching a broken tree would make the failure permanent for that key."""
    work = tmp_path / "job"
    work.mkdir()
    cache = SnapshotCache(str(tmp_path / "cache"))

    result = run_setup(
        script="exit 1",
        workdir=str(work),
        repo="o/n",
        backend=_FakeBackend(exit_code=1),
        cache=cache,
    )

    assert result.exit_code == 1
    assert cache.lookup(cache_key("o/n", "exit 1")) is None


@pytest.mark.parametrize("script", ["", "   ", "\n"])
def test_no_script_means_no_setup_phase(script: str, tmp_path: Path):
    """Most jobs have no setup, and must not pay for a phase they do not use."""
    backend = _FakeBackend()
    result = run_setup(
        script=script, workdir=str(tmp_path), repo="o/n", backend=backend, cache=None
    )
    assert not result.ran
    assert backend.specs == []
