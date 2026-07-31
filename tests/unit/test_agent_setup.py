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
    from collections.abc import Sequence
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


def test_the_key_follows_the_sandbox_image():
    """A tree with compiled dependencies is not portable across images.

    A `.venv` or a `node_modules` carrying native extensions is built against
    one interpreter and libc. Restoring it into a rebuilt sandbox hands the job
    a tree that fails at import time, a long way from the cause.
    """
    first = cache_key("o/n", "pip install -e .", image="sandbox:1")
    again = cache_key("o/n", "pip install -e .", image="sandbox:1")
    rebuilt = cache_key("o/n", "pip install -e .", image="sandbox:2")

    assert first == again
    assert first != rebuilt, "a snapshot built against one image was served to another"


def test_a_snapshot_round_trips(tmp_path: Path):
    """What setup produced is what a later job gets back."""
    work = tmp_path / "job1"
    (work / "node_modules" / "pkg").mkdir(parents=True)
    (work / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n")
    cache = SnapshotCache(str(tmp_path / "cache"))

    assert cache.save("k", str(work), entries=["node_modules"]) is True

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
    cache.save("k", str(work), entries=[".git", "vendor"])

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
    cache.save("k", str(work), entries=["dep.txt"])

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
    cache.save("fresh", str(work), entries=["f"])
    cache.save("stale", str(work), entries=["f"])
    stale = tmp_path / "cache" / "stale.tar"
    os.utime(stale, (time.time() - 3600, time.time() - 3600))

    assert cache.purge_expired() == 1
    assert (tmp_path / "cache" / "fresh.tar").exists()
    assert not stale.exists()


class _FakeBackend:
    """Records the spec it was asked to spawn.

    ``creates`` stands in for what a real setup script leaves in the worktree —
    a `node_modules`, a `.venv`. It is what the cache is allowed to capture, so
    a backend that creates nothing models the very common script that installs
    into the container instead.
    """

    def __init__(self, exit_code: int = 0, creates: Sequence[str] = ()) -> None:
        self.exit_code = exit_code
        self.creates = list(creates)
        self.specs: list = []

    def spawn(self, spec):
        self.specs.append(spec)
        for name in self.creates:
            os.makedirs(os.path.join(spec.workdir, name), exist_ok=True)
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
    cache.save(cache_key("o/n", "install"), str(work), entries=["vendor.txt"])

    second = tmp_path / "job2"
    second.mkdir()
    backend = _FakeBackend()
    result = run_setup(
        script="install", workdir=str(second), repo="o/n", backend=backend, cache=cache
    )

    assert result.restored_from_cache is True
    assert backend.specs == [], "a cache hit must not spawn anything"
    assert (second / "vendor.txt").read_text() == "installed\n"


def test_a_snapshot_never_carries_the_checkout_into_another_job(tmp_path: Path):
    """The cache key is the repository and script, so two commits share it.

    Capturing the whole worktree therefore meant the next job on that key had
    its freshly checked-out source overwritten by whatever the saving job
    happened to hold — a different branch, an older commit — silently, before
    the agent ever ran. Only what setup *created* may be captured.
    """
    first = tmp_path / "job1"
    (first / ".git").mkdir(parents=True)
    (first / "app.py").write_text("# turn one's source\n")
    cache = SnapshotCache(str(tmp_path / "cache"))

    run_setup(
        script="install",
        workdir=str(first),
        repo="o/n",
        backend=_FakeBackend(creates=["node_modules"]),
        cache=cache,
    )

    second = tmp_path / "job2"
    second.mkdir()
    (second / "app.py").write_text("# turn two's own checkout\n")
    result = run_setup(
        script="install", workdir=str(second), repo="o/n", backend=_FakeBackend(), cache=cache
    )

    assert result.restored_from_cache is True
    assert (second / "node_modules").is_dir(), "the dependency tree was not restored"
    assert (second / "app.py").read_text() == "# turn two's own checkout\n", (
        "the snapshot wrote another job's source over this job's checkout"
    )
    assert not (second / ".git").exists()


def test_a_setup_that_leaves_nothing_behind_is_not_cached(tmp_path: Path):
    """An empty snapshot restores cleanly, and so reads as a hit.

    Setup and the agent turn are separate throwaway containers sharing only
    the worktree, so a script installing outside it leaves nothing durable.
    Caching that would make every later job skip an install it never received
    — worse than a miss, because it looks like a success.
    """
    work = tmp_path / "job"
    work.mkdir()
    (work / "app.py").write_text("x = 1\n")
    cache = SnapshotCache(str(tmp_path / "cache"))
    assert cache.save("k", str(work), entries=[]) is False

    run_setup(
        script="pip install --user thing",
        workdir=str(work),
        repo="o/n",
        backend=_FakeBackend(),
        cache=cache,
    )
    assert cache.lookup(cache_key("o/n", "pip install --user thing")) is None

    second = tmp_path / "job2"
    second.mkdir()
    backend = _FakeBackend()
    result = run_setup(
        script="pip install --user thing",
        workdir=str(second),
        repo="o/n",
        backend=backend,
        cache=cache,
    )

    assert result.restored_from_cache is False
    assert backend.specs, "the install was skipped on the strength of an empty snapshot"


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


def test_saves_of_one_key_never_share_a_staging_path(tmp_path, monkeypatch):
    """Two writers of the same key must not write the same staging file.

    Replicas share AGENT_SNAPSHOT_ROOT, and two of them cold-starting the same
    repository and setup script hold the same cache key. With one fixed
    `.partial` name they truncate and interleave each other's archive, then
    rename the result into place, so later jobs restore a corrupt tree.

    The mechanism is asserted rather than the race: a thread test that only
    *sometimes* interleaves is green on the broken code too, which is no test
    at all. Distinct paths plus an atomic rename make concurrent saves
    last-writer-wins with a complete archive — correct, since both wrote the
    same content.
    """
    import tarfile as _tarfile

    from serving.agent_jobs.setup import SnapshotCache

    cache = SnapshotCache(root=tmp_path / "snapshots", ttl_seconds=3600)
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "deps.txt").write_text("installed", encoding="utf-8")

    seen: list[str] = []
    real_open = _tarfile.open

    def recording_open(name=None, mode="r", *args, **kwargs):
        if "w" in mode:
            seen.append(str(name))
        return real_open(name, mode, *args, **kwargs)

    monkeypatch.setattr("serving.agent_jobs.setup.tarfile.open", recording_open)

    assert cache.save("same-key", str(workdir), entries=["deps.txt"]) is True
    assert cache.save("same-key", str(workdir), entries=["deps.txt"]) is True

    assert len(seen) == 2
    assert seen[0] != seen[1], (
        "both writers staged through the same path, so concurrent saves of one "
        "key can truncate and interleave each other's archive"
    )
    # And the published archive is still complete and readable.
    restored = tmp_path / "restored"
    restored.mkdir()
    assert cache.restore("same-key", str(restored)) is True
    assert (restored / "deps.txt").read_text(encoding="utf-8") == "installed"


def test_abandoned_staging_files_are_swept(tmp_path):
    """A writer killed mid-archive must not leak its staging file forever."""
    import time as _time

    from serving.agent_jobs.setup import SnapshotCache

    root = tmp_path / "snapshots"
    root.mkdir()
    orphan = root / ".key.abcdef.tar.partial"
    orphan.write_bytes(b"half an archive")
    os.utime(orphan, (_time.time() - 7 * 3600, _time.time() - 7 * 3600))
    fresh = root / ".key.fedcba.tar.partial"
    fresh.write_bytes(b"still being written")

    SnapshotCache(root=root, ttl_seconds=3600).purge_expired()

    assert not orphan.exists(), "an abandoned staging file was never reclaimed"
    assert fresh.exists(), "a staging file a live job is still writing was deleted"
