"""Validation gate for agent-produced patches (issue #1041, P0).

The patch-out model puts *no* credential inside the sandbox: the agent emits a
patch, and a trusted publisher outside the sandbox applies it, pushes an
``agent/<job-id>`` branch and opens a draft PR. That makes this module the
security boundary — everything an untrusted agent can influence passes through
here before it becomes a git object.

Four gates, in the order they matter:

1. **Workflow files** (``.github/``). Pushing a branch that modifies a
   workflow can make GitHub execute it *with repository secrets* on the push
   event — before any human reads the draft PR. Draft-PR review is therefore
   not a mitigation for this class, so such patches are blocked and must be
   released by a human explicitly.
2. **Path escape.** ``..`` segments, absolute paths, and symlink-mode entries
   let a patch write outside the worktree or turn a file into a link to one.
3. **Secret scan.** An agent can inadvertently paste credentials it saw (env
   dumps, logs, a leaked token) into the diff; a match blocks publication.
4. **Size limits.** A patch larger than the cap, or touching more files than
   the cap, is refused rather than pushed — huge diffs are almost always a
   runaway agent, and they make review impossible anyway.

The gate parses ``git format-patch`` / ``git diff`` output textually rather
than applying it: it must be safe to run *before* the bytes touch a worktree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Limits ─────────────────────────────────────────────────────────────

MAX_PATCH_BYTES = 1_000_000
MAX_CHANGED_FILES = 200

# ── Blocked paths ──────────────────────────────────────────────────────

# `.github/` in full: workflows are the proven push-trigger escalation, and
# the rest of the directory (actions, CODEOWNERS, dependabot config) shapes CI
# and review authority, so an agent has no business editing any of it
# unattended.
_BLOCKED_PREFIXES = (".github/",)

# ── Secret patterns ────────────────────────────────────────────────────

# Deliberately narrow: each pattern targets a credential shape with a
# recognizable prefix or structure, so ordinary code and prose do not trip it.
# A false negative is caught by the human reviewing the draft PR; a false
# positive blocks a legitimate job, so precision wins here.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}")),
    ("gateway_key", re.compile(r"\bhyi-[A-Za-z0-9\-_]{20,}")),
    ("agent_worker_token", re.compile(r"\bajt\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)

# ── Diff parsing ───────────────────────────────────────────────────────

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_RENAME_TO = re.compile(r"^rename to (?P<path>.+)$")
_COPY_TO = re.compile(r"^copy to (?P<path>.+)$")
_NEW_MODE = re.compile(r"^(?:new|new file) mode (?P<mode>\d{6})$")
_SYMLINK_MODE = "120000"


@dataclass
class PatchGateResult:
    """Outcome of validating one patch."""

    ok: bool
    violations: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    requires_human_release: bool = False

    @property
    def reason(self) -> str:
        """Single-line summary suitable for a job's ``detail`` field."""
        return "; ".join(self.violations) if self.violations else "ok"


def _collect_paths(patch: str) -> tuple[list[str], list[str]]:
    """Return (changed paths, paths introduced with a symlink mode)."""
    changed: list[str] = []
    symlinks: list[str] = []
    pending_paths: list[str] = []

    for line in patch.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            pending_paths = [header.group("a"), header.group("b")]
            for path in pending_paths:
                if path not in changed:
                    changed.append(path)
            continue
        for pattern in (_RENAME_TO, _COPY_TO):
            match = pattern.match(line)
            if match:
                path = match.group("path")
                pending_paths.append(path)
                if path not in changed:
                    changed.append(path)
        mode = _NEW_MODE.match(line)
        if mode and mode.group("mode") == _SYMLINK_MODE:
            symlinks.extend(pending_paths)

    return changed, symlinks


def _is_escaping(path: str) -> bool:
    """Return whether a path escapes the repository root."""
    if path.startswith("/") or path.startswith("~"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", path):  # Windows drive-absolute
        return True
    return any(segment == ".." for segment in path.replace("\\", "/").split("/"))


def _scan_secrets(patch: str) -> list[str]:
    """Return the names of secret patterns found in added lines.

    Only ``+`` lines are scanned: an existing secret already in the repository
    is not this patch's doing, and flagging it would block every later job.
    """
    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(added)]


def validate_patch(
    patch: str,
    *,
    max_bytes: int = MAX_PATCH_BYTES,
    max_files: int = MAX_CHANGED_FILES,
) -> PatchGateResult:
    """Validate an agent-produced patch before it is allowed near git.

    Returns a :class:`PatchGateResult`; ``requires_human_release`` marks the
    workflow-file case, which a human may still choose to release after
    reading the diff, as opposed to hard violations like a leaked credential.
    """
    violations: list[str] = []
    requires_human_release = False

    if not patch.strip():
        return PatchGateResult(ok=False, violations=["patch is empty"])

    size = len(patch.encode("utf-8"))
    if size > max_bytes:
        violations.append(f"patch is {size} bytes, over the {max_bytes} byte limit")

    changed, symlinks = _collect_paths(patch)
    unique_files = sorted({path for path in changed if path != "/dev/null"})
    if not unique_files:
        violations.append("patch contains no recognizable file headers")
    if len(unique_files) > max_files:
        violations.append(f"patch touches {len(unique_files)} files, over the {max_files} limit")

    blocked = [
        path
        for path in unique_files
        if any(path.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
    ]
    if blocked:
        requires_human_release = True
        violations.append(
            "patch modifies CI/workflow configuration and needs explicit human "
            f"release: {', '.join(sorted(blocked))}"
        )

    escaping = [path for path in unique_files if _is_escaping(path)]
    if escaping:
        violations.append(f"patch writes outside the repository: {', '.join(sorted(escaping))}")

    symlink_paths = sorted({path for path in symlinks if path != "/dev/null"})
    if symlink_paths:
        violations.append(f"patch introduces symlinks: {', '.join(symlink_paths)}")

    secrets = _scan_secrets(patch)
    if secrets:
        violations.append(f"patch contains credential-shaped content: {', '.join(secrets)}")

    return PatchGateResult(
        ok=not violations,
        violations=violations,
        changed_files=unique_files,
        requires_human_release=requires_human_release,
    )


def branch_name_for(job_id: str) -> str:
    """Return the only branch name a job is ever allowed to publish to.

    Dots are dropped rather than kept: git rejects refs containing ``..`` or
    ending in ``.``, so a naive sanitizer that preserves them can emit a name
    git refuses. Job ids are generated (``ajob_<hex>``), so this is defensive
    against a hand-crafted id rather than a routine transformation.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", job_id).strip("-") or "unnamed"
    return f"agent/{safe}"
