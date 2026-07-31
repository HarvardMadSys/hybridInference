"""The setup phase and its filesystem snapshot cache (issue #1041).

Installing dependencies is the slow part of a job and the only part that needs
the network. The design separates it for both reasons:

- **It runs under its own egress tier.** Setup reaches a package registry; the
  agent turn afterwards does not, and the agent turn is the one driven by
  untrusted model output. Codex cloud makes the same cut.
- **Its result is cached.** The same repository with the same setup script
  produces the same tree, so a later job restores it instead of re-running
  ``pip install``. A retry after a reaped lease is the case that matters most:
  the design says resume must not re-run setup, and re-running it is also the
  most expensive thing a retry could do.

The cache key is the repository, a hash of the script, and the sandbox image —
not the commit. Dependencies change with the manifest, not with every commit,
so keying on the sha would miss almost every hit. The image is in the key
because the tree was built against it: a ``.venv`` or a ``node_modules``
holding compiled extensions is not portable to a different interpreter or
libc, and restoring one across an image change hands the job a tree that
imports but does not run. A stale entry costs a wrong dependency tree, so
entries expire (default seven days, as the design specifies) and the key
changes the moment the script does.

**A snapshot holds what the setup script produced, never the checkout it ran
against.** Setup and the agent turn are separate throwaway containers sharing
only the mounted worktree, so anything setup leaves behind durably is by
necessity a *new* entry in that worktree — which is exactly what is captured.
Archiving the whole tree instead (as this module first did) was wrong twice
over: restoring it wrote the saving job's source over the next job's freshly
checked-out commit, and it let a hit skip a setup whose real output was never
in the archive at all.

**Snapshots are per repository.** The tree they hold came out of one
repository's own setup, which may have run arbitrary code from that
repository; handing it to a job on another repository would be the same
mistake as sharing a worktree.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# Bumped when the meaning of a snapshot's *contents* changes, because the key
# is what decides whether an archive already on disk is still readable. Format
# 1 captured the whole worktree; serving one of those to this code would
# restore a stale checkout over a fresh one, so the bump strands them and the
# TTL sweep reclaims them.
_SNAPSHOT_FORMAT = 2

# How long an in-progress staging archive may sit before it is treated as
# abandoned. Comfortably longer than archiving a dependency tree takes, and far
# shorter than the cache TTL, which would let a stalled write look live for a
# week.
_STAGING_GRACE_SECONDS = 6 * 3600
# A snapshot exists to save time; one this large costs more to move than the
# install it replaces, and usually means the agent's own output got captured.
DEFAULT_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024

# Never captured into a snapshot. `.git` is the job's own checkout (a later job
# checks out its own commit), and the credential-bearing files that live there
# must not be carried into another job even though the runner already scrubs
# them. Belt and braces now that only what setup *created* is captured — `.git`
# predates setup, so it cannot reach the archive by that route either.
_EXCLUDED_TOP_LEVEL = (".git",)


@dataclass(frozen=True)
class SetupResult:
    """What the setup phase did, for the job's event log."""

    ran: bool
    restored_from_cache: bool
    exit_code: int = 0
    detail: str = ""


def cache_key(repo: str, script: str, *, image: str = "") -> str:
    """Key a snapshot by repository, script, and sandbox image — not by commit.

    Dependencies follow the manifest, not every commit, so keying on the sha
    would miss nearly every hit and make the cache pointless.

    ``image`` is the sandbox the script ran in. The tree it produced is built
    against that image, so a ``.venv`` or a ``node_modules`` carrying compiled
    extensions does not survive an image change — restoring one anyway hands
    the job a dependency tree that fails at import time, far from its cause.
    """
    digest = hashlib.sha256(f"{_SNAPSHOT_FORMAT}\n{repo}\n{script}\n{image}".encode()).hexdigest()[
        :32
    ]
    safe_repo = repo.replace("/", "__")
    return f"{safe_repo}-{digest}"


def _top_level_entries(workdir: str) -> set[str]:
    """The worktree's top-level names, for diffing what setup produced.

    A missing or unreadable worktree yields nothing rather than raising: this
    is used to decide what to cache, and failing to cache is never worse than
    failing the job that just installed successfully.
    """
    try:
        return set(os.listdir(workdir))
    except OSError:
        return set()


class SnapshotCache:
    """Stores and restores the tree a setup script produced."""

    def __init__(
        self,
        root: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
    ) -> None:
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes

    def _path_for(self, key: str) -> Path:
        return self.root / f"{key}.tar"

    def lookup(self, key: str) -> Path | None:
        """Return a live snapshot for this key, or None.

        An expired entry is removed rather than merely ignored: leaving it
        means a cache that only ever grows, on a host that also holds every
        job's worktree.
        """
        path = self._path_for(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.ttl_seconds:
            path.unlink(missing_ok=True)
            logger.info(
                "agent_snapshot_expired",
                extra={"event": "agent_snapshot_expired", "age_seconds": int(age)},
            )
            return None
        return path

    def save(self, key: str, workdir: str, *, entries: Sequence[str]) -> bool:
        """Capture the named top-level entries as this key's snapshot.

        ``entries`` is what the setup script *produced* — the caller diffs the
        worktree around the run and passes the difference. It is a required
        argument rather than an optional filter because the safe default does
        not exist: archiving "everything" is precisely the bug this replaced,
        where a restore laid one job's source over another job's checkout.

        Returns ``False`` when there is nothing to capture. An empty archive
        would restore cleanly and so read as a hit, which would make every
        later job skip a setup it never actually received.

        The staging file is unique per writer. Replicas share this directory,
        and two of them cold-starting the same repository and setup script hold
        the same cache key — with one fixed ``.partial`` name they truncated and
        interleaved each other's archive, then renamed it into place, so later
        jobs restored a corrupt tree. The rename stays atomic (same directory,
        same filesystem), which makes concurrent saves last-writer-wins with a
        *complete* archive: correct, because both wrote the same content.
        """
        # Plain names only. These come from `os.listdir`, so this rejects
        # nothing in practice — it keeps `save` safe for any future caller,
        # since an entry like `../x` would archive outside the worktree.
        capture = sorted(
            {
                entry
                for entry in entries
                if entry
                and entry not in _EXCLUDED_TOP_LEVEL
                and entry not in (os.curdir, os.pardir)
                and os.sep not in entry
                and (os.altsep is None or os.altsep not in entry)
            }
        )
        if not capture:
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path_for(key)
        handle, staged_name = tempfile.mkstemp(
            dir=self.root, prefix=f".{key}.", suffix=".tar.partial"
        )
        os.close(handle)
        staging = Path(staged_name)
        try:
            with tarfile.open(staging, "w") as archive:
                for entry in capture:
                    archive.add(os.path.join(workdir, entry), arcname=entry, recursive=True)
            if staging.stat().st_size > self.max_bytes:
                staging.unlink(missing_ok=True)
                logger.warning(
                    "agent_snapshot_too_large",
                    extra={"event": "agent_snapshot_too_large", "key": key},
                )
                return False
            # Rename last, and atomically: a reader must never find a
            # half-written archive, and several runners can be doing this at
            # once — which is why the staging name above is per writer.
            staging.replace(target)
        except OSError:
            staging.unlink(missing_ok=True)
            logger.warning("agent_snapshot_save_failed", exc_info=True)
            return False
        return True

    def restore(self, key: str, workdir: str) -> bool:
        """Unpack this key's snapshot over the workdir, if one is live."""
        path = self.lookup(key)
        if path is None:
            return False
        try:
            with tarfile.open(path, "r") as archive:
                for member in archive.getmembers():
                    # A snapshot is ours, but it was produced by running the
                    # repository's own setup script — so it is treated as
                    # untrusted input like everything else that tree touched.
                    if member.issym() or member.islnk():
                        continue
                    resolved = Path(workdir, member.name).resolve()
                    if not str(resolved).startswith(str(Path(workdir).resolve())):
                        logger.warning(
                            "agent_snapshot_path_escape",
                            extra={"event": "agent_snapshot_path_escape", "member": member.name},
                        )
                        return False
                archive.extractall(workdir)
        except (OSError, tarfile.TarError):
            logger.warning("agent_snapshot_restore_failed", exc_info=True)
            return False
        return True

    def purge_expired(self) -> int:
        """Drop every entry past its TTL. Returns how many were removed.

        Abandoned staging files are swept too. A writer killed mid-archive (a
        reaped lease, a restarted container) leaves one behind, and nothing else
        would ever remove it: they are never read, so they would accumulate
        silently until the disk filled. They get a fixed grace period rather
        than the cache TTL — a week-long window would let a stalled write look
        live — and one still being written by a live job is younger than that.
        """
        if not self.root.exists():
            return 0
        removed = 0
        cutoff = time.time() - self.ttl_seconds
        staging_cutoff = time.time() - _STAGING_GRACE_SECONDS
        for entry in self.root.glob("*.tar"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        for entry in self.root.glob("*.tar.partial"):
            try:
                if entry.stat().st_mtime < staging_cutoff:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue
        return removed


def run_setup(
    *,
    script: str,
    workdir: str,
    repo: str,
    backend: object,
    cache: SnapshotCache | None = None,
    timeout_s: float = 900.0,
) -> SetupResult:
    """Run the setup script for a job, reusing a snapshot when one exists.

    Executed under the *setup* egress tier by passing ``phase="setup"`` to the
    backend, so it can reach a package registry while the agent turn that
    follows cannot.

    What gets cached is the difference the script made to the worktree, taken
    around the run. Setup and the agent turn are separate throwaway containers
    sharing only this directory, so anything the script installs elsewhere is
    already gone by the time the agent starts — the worktree difference is
    both what survives and the only thing worth restoring.
    """
    if not script.strip():
        return SetupResult(ran=False, restored_from_cache=False)

    # The image is part of the key, so a tree with compiled dependencies is
    # never restored into a sandbox it was not built for. Read defensively:
    # the process backend used in tests and local runs has no image.
    image = str(getattr(backend, "image", "") or "")
    key = cache_key(repo, script, image=image)
    if cache is not None and cache.restore(key, workdir):
        logger.info("agent_setup_cache_hit", extra={"event": "agent_setup_cache_hit", "key": key})
        return SetupResult(ran=False, restored_from_cache=True, detail="restored from snapshot")

    from serving.agent_jobs.sandbox import SandboxSpec

    spec = SandboxSpec(
        argv=["/bin/sh", "-c", script],
        workdir=workdir,
        phase="setup",
        env={},
    )
    # Taken before the script runs: everything here belongs to the checkout,
    # and none of it may end up in the snapshot.
    pre_existing = _top_level_entries(workdir)
    process = backend.spawn(spec)  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout_s
    output: list[str] = []
    for line in process.lines():
        output.append(line)
        del output[:-100]
        if time.monotonic() > deadline:
            process.kill()
            return SetupResult(
                ran=True,
                restored_from_cache=False,
                exit_code=124,
                detail=f"setup exceeded {timeout_s:.0f}s",
            )
    exit_code = process.wait()
    if exit_code != 0:
        return SetupResult(
            ran=True,
            restored_from_cache=False,
            exit_code=exit_code,
            detail="".join(output)[-500:],
        )

    if cache is not None:
        produced = sorted(_top_level_entries(workdir) - pre_existing)
        if produced:
            cache.save(key, workdir, entries=produced)
        else:
            # Worth saying out loud rather than silently caching nothing: a
            # script that leaves no trace in the worktree installed into the
            # container instead, and that container is gone before the agent
            # starts. The job still runs — it just runs without the
            # dependencies its author thought they had installed.
            logger.info(
                "agent_setup_produced_nothing",
                extra={"event": "agent_setup_produced_nothing", "key": key},
            )
    return SetupResult(ran=True, restored_from_cache=False, detail="setup completed")


def build_cache_from_env(env: dict[str, str] | None = None) -> SnapshotCache | None:
    """Construct the snapshot cache, or None when it is not configured."""
    source = env if env is not None else dict(os.environ)
    root = (source.get("AGENT_SNAPSHOT_ROOT") or "").strip()
    if not root:
        return None
    try:
        ttl = float(source.get("AGENT_SNAPSHOT_TTL_S") or DEFAULT_TTL_SECONDS)
    except ValueError:
        ttl = DEFAULT_TTL_SECONDS
    return SnapshotCache(root, ttl_seconds=ttl)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "SetupResult",
    "SnapshotCache",
    "build_cache_from_env",
    "cache_key",
    "run_setup",
]
