"""Trusted publisher for agent-produced patches (issue #1041, P0).

Runs **outside** the sandbox. The agent never holds a git credential and never
pushes: it emits a patch, and this module validates it, applies it to a
throwaway clone, and pushes to the conversation's stable
``agent/<thread-id>`` branch.

Ordering is the whole point: :func:`~serving.agent_jobs.patch_gate.validate_patch`
runs *before* the bytes reach a worktree, and the push refspec is pinned to
the single computed branch — so neither a hostile patch nor a bug in the apply
step can write to ``dev``/``main`` or execute a workflow with repository
secrets.

Draft PR creation is left to the caller (it needs the GitHub App installation
token, which lives in the credential layer); this module returns the pushed
branch so the caller can open the PR against it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from serving.agent_jobs.patch_gate import (
    PatchGateResult,
    branch_name_for,
    validate_change_set,
    validate_patch,
)
from serving.utils.logging import get_logger

logger = get_logger(__name__)

_GIT_TIMEOUT_S = 120.0

# A commit hash and nothing else. git treats a leading `-` as an option even
# in positions that look like operands, so an unvalidated ref is an argument
# injection into a process that holds the repository credential.
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,64}")


# Hermetic git: no user config, no hooks, no credential helpers, no prompts.
# The identity these commits carry. A deployment sets its own; the defaults
# name the software rather than one site. Read from the runner's environment,
# which a patch cannot reach — the whole point of the dictionary below.
def _identity_name() -> str:
    return os.environ.get("AGENT_GIT_AUTHOR_NAME") or "HybridInference Agent"


def _identity_email() -> str:
    return os.environ.get("AGENT_GIT_AUTHOR_EMAIL") or "agent@localhost"


# A patch must not be able to reach configuration that changes what git does.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "GIT_COMMITTER_NAME": _identity_name(),
    "GIT_COMMITTER_EMAIL": _identity_email(),
    "GIT_AUTHOR_NAME": _identity_name(),
    "GIT_AUTHOR_EMAIL": _identity_email(),
}


class PublishError(Exception):
    """Raised when publication fails; the message is safe to store as detail."""


@dataclass
class PublishResult:
    """Outcome of publishing one job's patch."""

    branch: str
    commit_sha: str
    changed_files: list[str]


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> str:
    """Run one git command hermetically and return stdout."""
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", **_GIT_ENV, **(env_extra or {})}
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git {args[0]} timed out after {_GIT_TIMEOUT_S:.0f}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        raise PublishError(f"git {args[0]} failed: {tail}")
    return completed.stdout


@dataclass
class _StagedChangeSet:
    """What git reports is staged, as opposed to what the patch text implied."""

    paths: list[str]
    symlinks: list[str]


def _staged_change_set(repo: Path) -> _StagedChangeSet:
    """Read the staged paths and their modes straight out of the index.

    ``-z`` output is NUL-separated and never quoted, which is the point: the
    quoting git applies in a *diff header* is exactly what a hostile patch used
    to hide a path from the pre-apply gate.
    """
    raw = _run_git(["diff", "--cached", "--raw", "-z", "--no-renames"], cwd=repo)
    paths: list[str] = []
    symlinks: list[str] = []
    # `:<srcmode> <dstmode> <srcsha> <dstsha> <status>\0<path>\0`
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):
            index += 1
            continue
        if index + 1 >= len(fields):
            break
        path = fields[index + 1]
        parts = meta[1:].split()
        dst_mode = parts[1] if len(parts) > 1 else ""
        if path:
            paths.append(path)
            if dst_mode == "120000":
                symlinks.append(path)
        index += 2
    return _StagedChangeSet(paths=paths, symlinks=symlinks)


def publish_patch(
    *,
    job_id: str,
    patch: str,
    clone_url: str,
    base_sha: str,
    commit_message: str,
    branch_id: str | None = None,
    allow_workflow_changes: bool = False,
) -> PublishResult:
    """Validate and publish one run's patch on its stable thread branch.

    ``clone_url`` should already carry whatever short-lived credential the
    caller minted — it never leaves this process and never enters the sandbox.
    ``allow_workflow_changes`` is the explicit human release for patches that
    touch ``.github/``; it defaults to False so the gate holds by default.

    Raises :class:`PublishError` with a reviewer-readable message on any gate
    violation or git failure.
    """
    if not _COMMIT_SHA.fullmatch(base_sha or ""):
        # base_sha reaches here from user input on the job. Anything that is
        # not a bare commit hash is refused before it can be handed to git.
        raise PublishError(f"base_sha must be a full commit hash, got {base_sha!r}")

    gate = validate_patch(patch)
    if not _gate_permits(gate, allow_workflow_changes=allow_workflow_changes):
        raise PublishError(f"patch rejected: {gate.reason}")

    branch = branch_name_for(branch_id or job_id)
    workdir = Path(tempfile.mkdtemp(prefix=f"agent-publish-{job_id}-"))
    try:
        repo = workdir / "repo"
        # Fetch only the one commit the agent worked from: cheap, and it means
        # a patch cannot be applied onto some other branch's tree by accident.
        _run_git(["init", "--quiet", str(repo)])
        _run_git(["remote", "add", "origin", clone_url], cwd=repo)
        # `--end-of-options` plus the shape check above: git parses a leading
        # `--` argument as an option even after the remote name, so an
        # owner-supplied base_sha of `--upload-pack=/bin/sh -c ...` would run a
        # command *inside the trusted publisher*. Two independent stops,
        # because this process holds the GitHub credential.
        _run_git(
            ["fetch", "--quiet", "--depth", "1", "origin", "--end-of-options", base_sha],
            cwd=repo,
        )
        _run_git(["checkout", "--quiet", "-b", branch, "FETCH_HEAD"], cwd=repo)

        patch_file = workdir / "job.patch"
        patch_file.write_text(patch, encoding="utf-8")
        # `--3way` recovers from minor context drift; `--whitespace=nowarn`
        # keeps whitespace-only noise from failing the apply.
        _run_git(
            ["apply", "--3way", "--whitespace=nowarn", str(patch_file)],
            cwd=repo,
        )

        _run_git(["add", "--all"], cwd=repo)
        if not _run_git(["status", "--porcelain"], cwd=repo).strip():
            raise PublishError("patch applied cleanly but produced no changes")

        # Re-gate against what git says actually landed, not against the parse
        # of the patch text. The pre-apply gate gives an early rejection with a
        # readable reason, but a hostile patch only has to defeat the *parser*
        # to get past it — a C-quoted `.github/` path did exactly that. Here
        # the paths and modes come from the index itself, so there is nothing
        # left to misread.
        staged = _staged_change_set(repo)
        post = validate_change_set(staged.paths, staged.symlinks)
        if not _gate_permits(post, allow_workflow_changes=allow_workflow_changes):
            raise PublishError(f"patch rejected after apply: {post.reason}")
        gate = post
        _run_git(["commit", "--quiet", "--no-verify", "-m", commit_message], cwd=repo)
        commit_sha = _run_git(["rev-parse", "HEAD"], cwd=repo).strip()

        # Pinned refspec: this process can only ever write the one branch, and
        # never force-updates it.
        _run_git(
            ["push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
            cwd=repo,
        )
        logger.info(
            "agent_job_published",
            extra={
                "event": "agent_job_published",
                "job_id": job_id,
                "branch": branch,
                "files": len(gate.changed_files),
            },
        )
        return PublishResult(branch=branch, commit_sha=commit_sha, changed_files=gate.changed_files)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _gate_permits(gate: PatchGateResult, *, allow_workflow_changes: bool) -> bool:
    """Return whether a gate result may proceed to publication.

    The only overridable violation is the workflow-file block, and only when a
    human explicitly released it. Every other violation (secrets, path escape,
    symlinks, size) is unconditional.
    """
    if gate.ok:
        return True
    if not allow_workflow_changes or not gate.requires_human_release:
        return False
    remaining = [violation for violation in gate.violations if "human release" not in violation]
    return not remaining
