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

The cache key is the repository plus a hash of the script — not the commit.
Dependencies change with the manifest, not with every commit, so keying on the
sha would miss almost every hit. A stale entry costs a wrong dependency tree,
so entries expire (default seven days, as the design specifies) and the key
changes the moment the script does.

**Snapshots are per repository, and never shared across tenants.** The tree
they hold came out of one repository's own setup, which may have run arbitrary
code from that repository; handing it to another tenant would be the same
mistake as sharing a worktree.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from serving.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS = 7 * 24 * 3600
# A snapshot exists to save time; one this large costs more to move than the
# install it replaces, and usually means the agent's own output got captured.
DEFAULT_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024

# Never captured into a snapshot. `.git` is the job's own checkout (a later job
# checks out its own commit), and the credential-bearing files that live there
# must not be carried into another job even though the runner already scrubs
# them.
_EXCLUDED_TOP_LEVEL = (".git",)


@dataclass(frozen=True)
class SetupResult:
    """What the setup phase did, for the job's event log."""

    ran: bool
    restored_from_cache: bool
    exit_code: int = 0
    detail: str = ""


def cache_key(repo: str, script: str) -> str:
    """Key a snapshot by repository and script, not by commit.

    Dependencies follow the manifest, not every commit, so keying on the sha
    would miss nearly every hit and make the cache pointless.
    """
    digest = hashlib.sha256(f"{repo}\n{script}".encode()).hexdigest()[:32]
    safe_repo = repo.replace("/", "__")
    return f"{safe_repo}-{digest}"


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

    def save(self, key: str, workdir: str) -> bool:
        """Capture the workdir, minus the checkout, as this key's snapshot."""
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path_for(key)
        staging = target.with_suffix(".tar.partial")
        try:
            with tarfile.open(staging, "w") as archive:
                for entry in sorted(os.listdir(workdir)):
                    if entry in _EXCLUDED_TOP_LEVEL:
                        continue
                    archive.add(os.path.join(workdir, entry), arcname=entry, recursive=True)
            if staging.stat().st_size > self.max_bytes:
                staging.unlink(missing_ok=True)
                logger.warning(
                    "agent_snapshot_too_large",
                    extra={"event": "agent_snapshot_too_large", "key": key},
                )
                return False
            # Rename last: a reader must never find a half-written archive,
            # and several runners can be doing this at once.
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
        """Drop every entry past its TTL. Returns how many were removed."""
        if not self.root.exists():
            return 0
        removed = 0
        cutoff = time.time() - self.ttl_seconds
        for entry in self.root.glob("*.tar"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
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
    """
    if not script.strip():
        return SetupResult(ran=False, restored_from_cache=False)

    key = cache_key(repo, script)
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
        cache.save(key, workdir)
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
