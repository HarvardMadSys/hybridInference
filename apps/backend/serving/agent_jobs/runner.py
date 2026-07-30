"""The agent job runner (issue #1041, P0) — what actually executes a job.

Runs inside the sandbox (GitHub Actions for the P0 dogfood, a VM later) and is
the only component that ever touches agent output. Its shape follows from the
adjudicated security model rather than convenience:

- **One credential.** The dispatcher claims a job with its own credential and
  hands the runner a per-attempt capability token. That token is all the
  sandbox gets: it authorizes both event reporting *and* model calls, and it
  stops working the moment the job's fence moves. No GitHub token, no API key.
- **Patch-out, not push.** The runner never pushes. It emits a patch as an
  artifact; the trusted publisher outside the sandbox validates and pushes it.
  That is why the runner needs no git write credential at all.
- **Heartbeats are how cancellation arrives.** A background thread renews the
  lease and watches the reply for ``cancel_requested``; the agent is killed on
  the next beat. Losing the lease (409) is fatal by design — a runner whose
  attempt was superseded must stop rather than race its replacement.

Every step is written so that failing loudly beats continuing quietly: an
unparsable line becomes a raw event rather than disappearing, and a lost lease
ends the run instead of being retried.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import os
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from serving.agent_jobs.patch_gate import validate_patch
from serving.agent_jobs.runtimes import (
    EMPTY_RUNTIME_MCP_CONFIG,
    AgentRuntime,
    NormalizedEvent,
    RuntimeMCPConfig,
    get_runtime,
)
from serving.agent_jobs.sandbox import SandboxBackend, SandboxSpec, build_backend_from_env
from serving.agent_jobs.setup import build_cache_from_env, run_setup

DEFAULT_LEASE_TTL_S = 120.0
HEARTBEAT_INTERVAL_S = 30.0
DEFAULT_AGENT_TIMEOUT_S = 3600.0
# How often the control checks run while the agent is silent.
_POLL_INTERVAL_S = 0.5
_GIT_TIMEOUT_S = 300.0
# Bounds on the final in-sandbox `git diff`. Generous for a real patch, finite
# because the worktree's git configuration belongs to the agent by then.
_PATCH_TIMEOUT_S = 300.0
_PATCH_MAX_BYTES = 8 * 1024 * 1024

# Both are interpolated into a URL and handed to git, which reads a leading
# `-` as an option even where an operand belongs. Validate the shape here as
# well as at the API edge: this process holds the dispatcher credential.
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_REPO_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

# Hermetic git for anything the *runner* runs: no user config, no hooks, no
# credential helpers, no prompts.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "GIT_LFS_SKIP_SMUDGE": "1",
}

# git refuses to touch a repository owned by another user ("dubious
# ownership"). The runner normally hands the worktree over with a chown so the
# ids match, but an unprivileged runner cannot — and there the agent's own
# `git status` and our patch build would both abort. Set for the sandbox side
# only; it is not config the runner itself ever runs under.
_SAFE_DIRECTORY_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
}


# A blocked egress attempt looks like a connection failure in the agent's own
# tool output, because that is where it surfaces: on a deny-all network the
# kernel drops the packet and the tool reports the failure. Recording these is
# what the design asks P0 to produce — the evidence for what an external
# default should allow. Deliberately narrow: a name-resolution or connection
# error carrying a host, not every failing command.
_EGRESS_FAILURE = re.compile(
    r"(?:"
    r"Could not resolve host|Name or service not known|Temporary failure in name resolution"
    r"|Connection refused|Network is unreachable|No route to host|Connection timed out"
    r"|getaddrinfo (?:failed|ENOTFOUND)|ENOTFOUND|EAI_AGAIN"
    r")",
    re.IGNORECASE,
)
# Hosts as they appear in those messages, or in any URL alongside them.
_HOST_IN_TEXT = re.compile(
    r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})|"
    r"(?:host|ENOTFOUND|resolve)[:\s]+'?([A-Za-z0-9.-]+\.[A-Za-z]{2,})'?",
    re.IGNORECASE,
)


def detect_blocked_egress(text: str) -> str | None:
    """Return the host an agent failed to reach, if this reads as a denial.

    Returns ``None`` for anything that is merely a failed command: a false
    "the sandbox tried to phone home" is worse than a missed one, because the
    whole point of the record is to be evidence.
    """
    if not text or not _EGRESS_FAILURE.search(text):
        return None
    match = _HOST_IN_TEXT.search(text)
    if match is None:
        return None
    host = match.group(1) or match.group(2)
    return host.strip("'\"").lower() or None


class LeaseLost(Exception):
    """Raised when this attempt no longer owns the job and must stop."""


class ClaimUnreachable(Exception):
    """The gateway could not be reached *to claim*, so no job was taken.

    Distinct from a transport failure later in the run: that one leaves an
    attempt running until its lease expires, and an operator needs to be sent
    to the job rather than told nothing was claimed.
    """


class WorktreeError(Exception):
    """Raised when the job's repository could not be materialized."""


@dataclass
class ClaimedJob:
    """What the dispatcher handed us."""

    job_id: str
    attempt_id: int
    attempt_no: int
    repo: str
    base_sha: str | None
    task_prompt: str
    runtime: str
    model: str
    worker_token: str
    # Model-scoped credential: the only one that enters the sandbox.
    sandbox_token: str
    # Shell run before the agent, under the setup egress tier. Optional, so it
    # carries a default rather than forcing every caller to pass one.
    setup_script: str | None = None
    # Read-only, single-repo, short-lived: what the runner clones with. Stays
    # in the runner — it is never put in .git/config and never enters the
    # sandbox environment. ``None`` for a public repository.
    clone_token: str | None = None
    thread_id: str | None = None
    parent_job_id: str | None = None
    turn_no: int = 1
    # Prior platform messages are runtime-neutral. They let a follow-up keep
    # its conversational context even when the next turn switches harnesses.
    context_messages: list[dict[str, str]] = field(default_factory=list)
    # The prior successful run's edits, re-applied before the next sandbox is
    # started. Credentials still never cross the sandbox boundary.
    context_patch: str | None = None

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> ClaimedJob:
        """Build from the claim endpoint's response."""
        return cls(
            job_id=body["job_id"],
            thread_id=body.get("thread_id"),
            parent_job_id=body.get("parent_job_id"),
            turn_no=int(body.get("turn_no") or 1),
            attempt_id=body["attempt_id"],
            attempt_no=body["attempt_no"],
            repo=body["repo"],
            base_sha=body.get("base_sha"),
            task_prompt=body["task_prompt"],
            setup_script=body.get("setup_script"),
            runtime=body["runtime"],
            model=body["model"],
            worker_token=body["worker_token"],
            # Older gateways return only worker_token; fall back so a
            # runner can still talk to one that predates scoped tokens.
            sandbox_token=body.get("sandbox_token") or body["worker_token"],
            clone_token=body.get("clone_token") or None,
            context_messages=list(body.get("context_messages") or []),
            context_patch=body.get("context_patch") or None,
        )


class ControlPlane:
    """Thin client for the worker endpoints, using the capability token."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})

    def close(self) -> None:
        """Release the HTTP connection pool."""
        self._client.close()

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(f"{self._base}{path}", json=payload or {})
        if response.status_code == 409:
            raise LeaseLost(response.text[:200])
        response.raise_for_status()
        return response.json() if response.content else {}

    def append_event(self, event: NormalizedEvent) -> None:
        """Report one normalized event."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/events",
            {"event_type": event.event_type, "payload": event.payload},
        )

    def heartbeat(self, ttl: float) -> dict[str, Any]:
        """Renew the lease and learn whether cancellation is pending."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/heartbeat", {"lease_ttl_seconds": ttl}
        )

    def save_artifact(self, kind: str, content: str) -> None:
        """Store an artifact produced by this attempt."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/artifacts",
            {"kind": kind, "content": content},
        )

    def finish(self, state: str, detail: str | None = None, base_sha: str | None = None) -> None:
        """Drive the fenced terminal transition.

        ``base_sha`` reports the commit the agent actually worked from. A job
        may be submitted without one, and the publisher cannot apply a patch
        without knowing its base — so the runner, which resolves it when it
        checks the repository out, is the component that knows.
        """
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/finish",
            {"state": state, "detail": detail, "base_sha": base_sha},
        )

    job_id: str = ""


class Heartbeater:
    """Renews the lease in the background and surfaces cancellation."""

    def __init__(self, control: ControlPlane, *, ttl: float, interval: float) -> None:
        self._control = control
        self._ttl = ttl
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cancel_requested = False
        self.lease_lost = False

    def start(self) -> None:
        """Begin heartbeating on a daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        """Stop heartbeating and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                reply = self._control.heartbeat(self._ttl)
            except LeaseLost:
                # Superseded or reaped: the replacement attempt owns the job now.
                self.lease_lost = True
                return
            except Exception:
                # A transient control-plane blip must not kill a healthy run;
                # if it persists, the lease expires and the reaper takes over.
                continue
            if reply.get("cancel_requested"):
                self.cancel_requested = True
                return


def claim(
    *, base_url: str, dispatcher_token: str, worker_id: str, lease_ttl: float
) -> ClaimedJob | None:
    """Claim the next queued job with the dispatcher credential.

    The dispatcher credential is used here and nowhere else — it does not
    travel into the agent's environment.
    """
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/agent/worker/claim",
            json={"worker_id": worker_id, "lease_ttl_seconds": lease_ttl},
            headers={"Authorization": f"Bearer {dispatcher_token}"},
            timeout=30.0,
        )
    except httpx.TransportError as exc:
        # Tagged at the one place where "the gateway is unreachable" also means
        # "no job was claimed". The same error from a later call — an event, an
        # artifact, a terminal transition — happens with an attempt already
        # running, and reporting *that* as an unreached gateway would hide the
        # job an operator has to go look at.
        raise ClaimUnreachable(str(exc)) from exc
    response.raise_for_status()
    body = response.json()
    return ClaimedJob.from_response(body) if body else None


def _run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    env_extra: dict[str, str] | None = None,
    input_text: str | None = None,
):
    """Run one git command hermetically and return the completed process."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **_GIT_ENV, **(env_extra or {})}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        stdin=subprocess.DEVNULL if input_text is None else None,
        input=input_text,
        check=False,
    )


def apply_context_patch(workdir: str, patch: str) -> None:
    """Safely rehydrate a successful parent run into a fresh worktree."""
    gate = validate_patch(patch)
    if not gate.ok:
        raise WorktreeError(
            "refusing unsafe parent context patch: " + "; ".join(gate.violations[:3])
        )
    applied = _run_git(["apply", "--whitespace=nowarn"], cwd=workdir, input_text=patch)
    if applied.returncode != 0:
        raise WorktreeError(f"could not restore parent changes: {applied.stderr.strip()[:500]}")


def conversation_prompt(job: ClaimedJob, *, max_context_chars: int = 40_000) -> str:
    """Build a bounded, runtime-neutral prompt for a follow-up turn."""
    if not job.context_messages:
        return job.task_prompt
    rendered: list[str] = []
    remaining = max_context_chars
    # Prefer the newest context when a long-running thread exceeds the bound.
    for message in reversed(job.context_messages):
        role = str(message.get("role") or "user").upper()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        chunk = f"{role}: {content}"
        if len(chunk) > remaining:
            chunk = chunk[-remaining:]
        rendered.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    rendered.reverse()
    history = "\n\n".join(rendered)
    return (
        "Continue an existing coding task in the current worktree. Previous successful "
        "edits have already been restored. Preserve them unless the new request asks otherwise.\n\n"
        f"Conversation so far:\n{history}\n\nNEW USER REQUEST:\n{job.task_prompt}"
    )


def _auth_env(clone_token: str | None) -> dict[str, str]:
    """Carry the clone credential in git's environment, never in argv.

    ``GIT_CONFIG_COUNT`` sets config for this invocation only. That matters
    twice over: the token never lands in ``.git/config`` (where the agent would
    read it out of the worktree we are about to mount), and it never appears in
    the process arguments (where any other user on the runner host could see it
    in ``ps`` for the duration of the fetch).
    """
    if not clone_token:
        return {}
    basic = base64.b64encode(f"x-access-token:{clone_token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _assert_no_credential_on_disk(workdir: str, clone_token: str | None) -> None:
    """Fail rather than mount a worktree that carries the clone credential.

    The whole patch-out design rests on no repository credential reaching the
    sandbox, and this tree is about to be bind-mounted into it. Asserting the
    property beats assuming it: git writes several files during a fetch, and a
    future change to how we authenticate could quietly start recording the
    token in one of them.
    """
    if not clone_token:
        return
    git_dir = pathlib.Path(workdir) / ".git"
    for path in git_dir.rglob("*"):
        # Object and pack files hold repository content, not our configuration,
        # and are compressed; skipping them keeps this check bounded.
        if not path.is_file() or path.is_symlink() or "objects" in path.parts:
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if clone_token in text:
            raise WorktreeError(
                f"refusing to run: the clone credential was written to .git/{path.name}, "
                "which the sandbox would be able to read"
            )


def prepare_worktree(
    *,
    workdir: str,
    repo: str,
    base_sha: str | None,
    clone_token: str | None = None,
    remote_base: str = "https://github.com",
) -> str:
    """Materialize the job's repository in ``workdir`` and return its commit.

    Runs in the **trusted runner**, and deliberately before the agent starts:
    at this point the worktree is ours, not the agent's, so running git here is
    safe in a way that :func:`build_patch` afterwards is not.

    A shallow single-commit fetch, with no remote recorded. Not adding a remote
    is the point — it is what keeps the credential out of ``.git/config``, and
    the runner has no reason to offer the agent a push path it must not use.
    """
    if not _REPO_SLUG.fullmatch(repo or ""):
        raise WorktreeError(f"repo must be 'owner/name', got {repo!r}")
    if base_sha and not _COMMIT_SHA.fullmatch(base_sha):
        raise WorktreeError(f"base_sha must be a commit hash, got {base_sha!r}")

    url = f"{remote_base.rstrip('/')}/{repo}.git"
    auth = _auth_env(clone_token)

    init = _run_git(["init", "--quiet", workdir])
    if init.returncode != 0:
        raise WorktreeError(f"git init failed: {init.stderr.strip()[:200]}")

    # `--end-of-options` so a ref that survived the shape check above still
    # cannot be parsed as an option by a git version we did not anticipate.
    ref = base_sha or "HEAD"
    fetch = _run_git(
        ["fetch", "--quiet", "--depth", "1", url, "--end-of-options", ref],
        cwd=workdir,
        env_extra=auth,
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip().splitlines()
        raise WorktreeError(
            f"could not fetch {repo}@{ref}: {detail[-1] if detail else 'no output'}"
        )

    checkout = _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=workdir)
    if checkout.returncode != 0:
        raise WorktreeError(f"could not check out {ref}: {checkout.stderr.strip()[:200]}")

    resolved = _run_git(["rev-parse", "HEAD"], cwd=workdir).stdout.strip()
    _assert_no_credential_on_disk(workdir, clone_token)
    return resolved or (base_sha or "")


def existing_checkout_sha(workdir: str, repo: str) -> str | None:
    """Return the checked-out commit if ``workdir`` already holds ``repo``.

    The GitHub Actions dogfood checks the repository out itself, so the runner
    must use that worktree rather than clone over it. Returns ``None`` when
    there is nothing usable here, and raises when a worktree exists but is a
    *different* repository — running a job against the wrong repo would produce
    a patch that looks plausible and applies to nothing.
    """
    if not (pathlib.Path(workdir) / ".git").exists():
        return None
    remote = _run_git(["config", "--get", "remote.origin.url"], cwd=workdir).stdout.strip()
    if remote:
        # Exact `owner/name`, not a suffix: `endswith` accepted a checkout of
        # `acme/foo` for a job targeting `me/foo`, and the agent would then run
        # against the wrong codebase and produce a patch that applies to
        # nothing.
        slug = remote.removesuffix(".git").rsplit(":", 1)[-1].strip("/")
        slug = "/".join(slug.split("/")[-2:])
        if slug.lower() != repo.lower():
            raise WorktreeError(
                f"the working tree holds {slug!r} but this job targets {repo!r}; "
                "refusing to run an agent against the wrong repository"
            )
    head = _run_git(["rev-parse", "HEAD"], cwd=workdir)
    return head.stdout.strip() if head.returncode == 0 else None


def align_existing_checkout(workdir: str, *, checked_out: str, base_sha: str | None) -> str:
    """Move a pre-populated worktree onto the commit the job pinned.

    The Actions runner checks out whatever ref triggered the workflow, which
    has nothing to do with the commit an owner pinned on their job. Running the
    agent on the wrong tree is not a visible failure: the store keeps the
    owner's pinned sha, and the publisher then applies a patch generated
    against one commit onto a different one. ``--3way`` makes that *usually*
    succeed, which is worse than failing — the PR looks plausible and encodes
    changes nobody wrote.

    Returns the commit actually in the worktree afterwards.
    """
    if not base_sha:
        return checked_out
    if not _COMMIT_SHA.fullmatch(base_sha):
        raise WorktreeError(f"base_sha must be a commit hash, got {base_sha!r}")

    # The owner may have pinned an abbreviated sha; compare resolved commits.
    resolved = _run_git(["rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}"], cwd=workdir)
    target = resolved.stdout.strip()
    if resolved.returncode != 0 or not target:
        raise WorktreeError(
            f"the prepared working tree does not contain {base_sha}; refusing to run "
            "the agent against a different commit than the job pinned"
        )
    if target == checked_out:
        return checked_out

    switched = _run_git(["checkout", "--quiet", "--detach", target], cwd=workdir)
    if switched.returncode != 0:
        raise WorktreeError(
            f"could not check out the pinned commit {base_sha}: {switched.stderr.strip()[:200]}"
        )
    return target


def build_patch(workdir: str, backend: SandboxBackend | None = None) -> str:
    """Return the agent's work as a patch, or an empty string if nothing changed.

    Runs git **inside the sandbox**, never in the trusted runner. The agent
    owns this worktree, including its ``.git/config`` — and git reads that
    file. A trusted process running ``git diff`` here would execute whatever
    the agent put in ``diff.external`` (or a textconv filter, or
    ``core.fsmonitor``), with the runner's privileges: the dispatcher
    credential, and in the Compose deployment the Docker socket. Generating
    the patch in the same container the agent already ran in keeps that
    execution inside the boundary that was built for it.

    ``git add -A -N`` stages intents so new files appear in the diff. Nothing
    is committed and nothing is pushed: the trusted publisher owns that side,
    and it re-validates the patch before it touches a repository.
    """
    script = "git add -A -N >/dev/null 2>&1; git diff --binary HEAD"
    if backend is None:
        # No sandbox available (unit tests, an operator running by hand). Run
        # with config sources neutralised — this is weaker than the sandbox,
        # because repo-local .git/config is still honoured by git, so it is
        # only appropriate where the worktree is already trusted.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        result = subprocess.run(
            ["/bin/sh", "-c", script],
            cwd=workdir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout

    process = backend.spawn(
        SandboxSpec(argv=["/bin/sh", "-c", script], workdir=workdir, env=_SAFE_DIRECTORY_ENV)
    )
    return _read_bounded(process, deadline_s=_PATCH_TIMEOUT_S, max_bytes=_PATCH_MAX_BYTES)


def _read_bounded(process: Any, *, deadline_s: float, max_bytes: int) -> str:
    """Drain a sandbox process's stdout under a deadline and a byte cap.

    This step still runs git against a worktree the agent owned for the whole
    run, including its ``.git/config``. A ``diff.external`` (or a textconv
    filter, or ``core.fsmonitor``) that never returns would otherwise block
    here forever — and because the heartbeat thread keeps renewing the lease
    until the enclosing ``finally``, the attempt never expires, the reaper
    never takes it, and a ``--loop`` runner claims nothing further. One hostile
    repository would take a runner out permanently.
    """
    chunks: list[str] = []
    total = 0
    lines: queue.Queue[str | None] = queue.Queue()

    def _pump() -> None:
        try:
            for line in process.lines():
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=_pump, daemon=True, name="agent-patch").start()
    deadline = time.monotonic() + deadline_s
    while True:
        if time.monotonic() > deadline:
            process.kill()
            raise WorktreeError(
                f"building the patch exceeded {deadline_s:.0f}s; the working tree's git "
                "configuration is agent-controlled and may be hostile"
            )
        try:
            line = lines.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            continue
        if line is None:
            break
        total += len(line.encode())
        if total > max_bytes:
            process.kill()
            raise WorktreeError(
                f"the patch exceeded {max_bytes} bytes while being read; refusing to "
                "buffer unbounded agent output"
            )
        chunks.append(line)

    process.wait()
    return "".join(chunks)


def run_agent(
    runtime: AgentRuntime,
    *,
    job: ClaimedJob,
    workdir: str,
    gateway_base_url: str,
    control: ControlPlane,
    heart: Heartbeater,
    timeout_s: float,
    backend: SandboxBackend,
    mcp_config: RuntimeMCPConfig = EMPTY_RUNTIME_MCP_CONFIG,
) -> tuple[int, str, list[str]]:
    """Run the agent, streaming its output back as normalized events.

    Returns ``(exit_code, tail, tool_errors)``. Raises :class:`LeaseLost` if this attempt
    stops owning the job mid-run.
    """
    argv, extra_env = runtime.prepare(
        workdir=workdir,
        task_prompt=conversation_prompt(job),
        model=job.model,
        gateway_base_url=gateway_base_url,
        # Only the model-scoped credential crosses into the sandbox. The full
        # capability token — which can write events, artifacts and terminal
        # states — stays out here with the runner, so an agent that leaks its
        # credential can spend the job's capped budget and nothing more.
        credential=job.sandbox_token,
        # The runner is the only future source of this value. Repository
        # contents never become runtime MCP configuration, and every adapter
        # currently rejects a non-empty set until the gateway broker exists.
        mcp_config=mcp_config,
    )

    # Hermetic environment, and the *backend's* idea of it rather than the
    # runner's. For an isolated backend the runner's PATH and HOME name paths
    # inside the runner's own filesystem: forwarding HOME=/root into a
    # container that runs unprivileged makes every agent CLI fail on startup
    # trying to write its config. Inheriting the whole environment would be
    # worse still — it would hand the agent whatever the host is carrying.
    env = dict(backend.base_env())
    env.update(_SAFE_DIRECTORY_ENV)
    env.update(extra_env)

    process = backend.spawn(SandboxSpec(argv=argv, workdir=workdir, env=env))

    deadline = time.monotonic() + timeout_s
    tail: list[str] = []
    # Tool failures the agent may or may not acknowledge. A model that says "I
    # added the docstring" after its Edit was denied is a real behaviour we
    # observed, so the runner reports what the tools did rather than trusting
    # the agent's summary.
    tool_errors: list[str] = []
    # One row per host, not per retry: an agent that retries a blocked address
    # ten times tried to reach one place.
    denied_hosts: set[str] = set()

    # Read stdout on a separate thread and poll for it here, so cancellation
    # and the deadline are honoured even when the agent goes quiet. Iterating
    # the stream directly meant a hung or long-thinking agent — which produces
    # no lines — was never checked, and ignored an owner's cancel until it
    # happened to print something.
    lines: queue.Queue[str | None] = queue.Queue()

    def _pump() -> None:
        try:
            for line in process.lines():
                lines.put(line)
        finally:
            lines.put(None)

    pump = threading.Thread(target=_pump, daemon=True, name="agent-stdout")
    pump.start()

    while True:
        if heart.lease_lost:
            process.kill()
            raise LeaseLost("lease lost while the agent was running")
        if heart.cancel_requested:
            process.kill()
            control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
            return 130, "".join(tail), tool_errors
        if time.monotonic() > deadline:
            process.kill()
            control.append_event(
                NormalizedEvent("error", {"text": f"agent exceeded {timeout_s:.0f}s"})
            )
            return 124, "".join(tail), tool_errors

        try:
            line = lines.get(timeout=_POLL_INTERVAL_S)
        except queue.Empty:
            # No output yet; loop so the checks above keep running.
            continue
        if line is None:
            break

        tail.append(line)
        del tail[:-40]
        event = runtime.parse_event(line)
        if event is not None:
            if event.event_type == "tool_result" and (event.payload or {}).get("is_error"):
                content = str((event.payload or {}).get("content", ""))
                tool_errors.append(content[:200])
                blocked = detect_blocked_egress(content)
                if blocked and blocked not in denied_hosts:
                    denied_hosts.add(blocked)
                    # `host` is what makes this a first-class egress row in the
                    # owner's stream rather than one more failed command.
                    control.append_event(NormalizedEvent("error", {"host": blocked}))
            control.append_event(event)

    exit_code = process.wait()
    stderr = process.stderr_text()
    if stderr.strip():
        tail.append(stderr[-2000:])
    if tool_errors:
        # Surfaced as an event so the owner sees it in the stream, not only in
        # the final detail string.
        control.append_event(
            NormalizedEvent(
                "error",
                {"detail": f"{len(tool_errors)} tool call(s) failed", "first": tool_errors[0]},
            )
        )
    return exit_code, "".join(tail), tool_errors


def run_once(
    *,
    base_url: str,
    dispatcher_token: str,
    worker_id: str,
    workdir: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    generic_command: str | None = None,
    backend: SandboxBackend | None = None,
) -> int:
    """Claim one job, run it, and report the outcome. Returns a process exit code.

    ``backend`` decides where the agent actually executes. When this function
    builds it, it is also preflighted here — a misconfigured host then fails
    without first taking a job off the queue and burning one of its attempts.

    A caller that *supplies* a backend has already preflighted it, and must
    not have it re-checked per call. Preflight spawns several ``docker``
    subprocesses (version, one network inspect per phase) and logs the
    shared-kernel warning, so running it per claim meant a standing runner
    polling every 5s spent ~17k probes and 17k warning lines a day doing
    nothing — found by watching an idle runner, not by reading it.
    """
    if backend is None:
        backend = build_backend_from_env()
        backend.preflight()

    job = claim(
        base_url=base_url,
        dispatcher_token=dispatcher_token,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
    )
    if job is None:
        print("no queued agent job; nothing to do")
        return 0

    print(f"claimed {job.job_id} attempt {job.attempt_no} runtime={job.runtime}")
    control = ControlPlane(base_url, job.worker_token)
    control.job_id = job.job_id
    heart = Heartbeater(control, ttl=lease_ttl, interval=HEARTBEAT_INTERVAL_S)

    try:
        runtime = get_runtime(job.runtime, generic_command=generic_command)
    except KeyError as exc:
        control.finish("failed", str(exc))
        control.close()
        return 2

    # Ask the backend, not this host: with an isolated backend the agent CLIs
    # live in the sandbox image, so probing the runner's own PATH would refuse
    # every job on a correctly configured machine.
    if not backend.has_binary(runtime.binary):
        # Fail the job explicitly rather than letting it hang in `running`
        # until the reaper eventually gives up on it.
        control.append_event(
            NormalizedEvent("error", {"text": f"runtime binary {runtime.binary!r} missing"})
        )
        control.finish("failed", f"runtime binary {runtime.binary!r} is not installed")
        control.close()
        return 2

    heart.start()
    try:
        control.append_event(
            NormalizedEvent(
                "lifecycle",
                {"phase": "started", "runtime": job.runtime, "attempt_no": job.attempt_no},
            )
        )

        # Give the agent something to work on. Without this the self-hosted
        # runner ran every job in an empty directory: the agent had no code to
        # read, and the patch it produced was necessarily empty.
        try:
            checked_out = existing_checkout_sha(workdir, job.repo)
            if checked_out is None:
                base_sha = prepare_worktree(
                    workdir=workdir,
                    repo=job.repo,
                    base_sha=job.base_sha,
                    clone_token=job.clone_token,
                )
            else:
                # An externally prepared worktree (the Actions dogfood checks
                # the repository out itself) — but on whatever ref triggered
                # the workflow, which is not necessarily what the job pinned.
                base_sha = align_existing_checkout(
                    workdir, checked_out=checked_out, base_sha=job.base_sha
                )
            # The agent owns this tree from here on, so it must be able to
            # write to it — and git must not see it as another user's repo.
            backend.adopt_workdir(workdir)
        except WorktreeError as exc:
            control.append_event(NormalizedEvent("error", {"text": str(exc)}))
            control.finish("failed", str(exc))
            return 2
        control.append_event(
            NormalizedEvent("lifecycle", {"phase": "checked_out", "base_sha": base_sha})
        )

        if job.context_patch:
            try:
                apply_context_patch(workdir, job.context_patch)
            except WorktreeError as exc:
                control.append_event(NormalizedEvent("error", {"text": str(exc)}))
                control.finish("failed", str(exc), base_sha=base_sha)
                return 2
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {"phase": "context_restored", "parent_job_id": job.parent_job_id},
                )
            )

        # Setup runs before the agent and under its own egress tier: it needs
        # a package registry, and the turn that follows — the one driven by
        # untrusted model output — does not.
        if job.setup_script:
            setup = run_setup(
                script=job.setup_script,
                workdir=workdir,
                repo=job.repo,
                backend=backend,
                cache=build_cache_from_env(),
            )
            control.append_event(
                NormalizedEvent(
                    "lifecycle",
                    {
                        "phase": "setup",
                        "cached": setup.restored_from_cache,
                        "ran": setup.ran,
                    },
                )
            )
            if setup.exit_code != 0:
                # A job whose dependencies did not install cannot do the work,
                # and letting the agent start anyway produces a confusing
                # failure much later, in the model's voice rather than the
                # installer's.
                control.finish(
                    "failed",
                    f"setup failed ({setup.exit_code}): {setup.detail}",
                    base_sha=base_sha,
                )
                return 1

        exit_code, tail, tool_errors = run_agent(
            runtime,
            job=job,
            workdir=workdir,
            gateway_base_url=base_url,
            control=control,
            heart=heart,
            timeout_s=agent_timeout_s,
            backend=backend,
        )

        if exit_code == 130:
            control.finish("cancelled", "cancelled by owner", base_sha=base_sha)
            return 0

        patch = build_patch(workdir, backend)
        if patch.strip():
            control.save_artifact("patch", patch)
            control.append_event(
                NormalizedEvent("diff", {"bytes": len(patch.encode()), "stored": True})
            )
        else:
            control.append_event(NormalizedEvent("diff", {"bytes": 0, "stored": False}))

        if exit_code != 0:
            control.finish("failed", f"agent exited {exit_code}: {tail[-500:]}", base_sha=base_sha)
            return 1

        # The publisher runs outside the sandbox and drives publish/*; the
        # runner's job ends at a validated patch. Leaving the job `running`
        # would be a lie, so report success and let the publisher take it from
        # here when a patch exists.
        if not patch.strip():
            # No patch is only success if nothing went wrong. A denied write
            # followed by an agent claiming it succeeded must not be recorded
            # as a clean run — the owner would see "succeeded" on a job that
            # did nothing and never learn why.
            if tool_errors:
                control.finish(
                    "failed",
                    f"agent produced no changes after {len(tool_errors)} failed "
                    f"tool call(s): {tool_errors[0]}",
                    base_sha=base_sha,
                )
                return 1
            control.finish("succeeded", "agent completed (no changes)", base_sha=base_sha)
            return 0

        control.finish("succeeded", "agent completed", base_sha=base_sha)
        return 0
    except WorktreeError as exc:
        # Bounded patch generation raises this on a timeout or byte cap. Left
        # to escape, the job stayed `running` until its lease expired and was
        # then retried — repeating a deterministic failure until the attempt
        # budget ran out, with nothing in the record saying why.
        print(f"patch generation failed: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            control.append_event(NormalizedEvent("error", {"text": str(exc)}))
            control.finish("failed", str(exc), base_sha=base_sha)
        return 2
    except LeaseLost as exc:
        print(f"lease lost, stopping: {exc}", file=sys.stderr)
        return 3
    finally:
        heart.stop()
        control.close()


def run_forever(
    *,
    base_url: str,
    dispatcher_token: str,
    worker_id: str,
    workdir_root: str,
    lease_ttl: float = DEFAULT_LEASE_TTL_S,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    generic_command: str | None = None,
    idle_sleep_s: float = 5.0,
    backend: SandboxBackend | None = None,
) -> int:
    """Claim and run jobs until interrupted — the long-lived runner.

    Each job gets a fresh directory under ``workdir_root`` which is removed
    afterwards, so one job can never read or corrupt another's worktree even
    when they share a host.

    A failure in one job must not take the runner down: the store already
    records the outcome against that job, and a runner that exits on the first
    bad job turns one broken repository into an outage for every queued job.
    """
    backend = backend or build_backend_from_env()
    root = pathlib.Path(workdir_root)
    root.mkdir(parents=True, exist_ok=True)
    # Pass the workdir root so a backend that bind-mounts it can prove the
    # mount works now, rather than failing every job with an opaque error.
    backend.preflight(workdir_root=str(root))
    # The sandbox must be able to reach the gateway it will be handed. Checked
    # once here rather than discovered per job as an opaque model failure.
    checker = getattr(backend, "check_gateway_reachable", None)
    if checker is not None:
        checker(base_url)
    print(f"agent runner {worker_id} started (sandbox backend: {backend.name})", flush=True)

    while True:
        job_dir = pathlib.Path(tempfile.mkdtemp(prefix="job-", dir=str(root)))
        try:
            run_once(
                base_url=base_url,
                dispatcher_token=dispatcher_token,
                worker_id=worker_id,
                workdir=str(job_dir),
                lease_ttl=lease_ttl,
                agent_timeout_s=agent_timeout_s,
                generic_command=generic_command,
                backend=backend,
            )
        except KeyboardInterrupt:
            return 0
        except ClaimUnreachable as exc:
            # Raised only by the claim call, so this really does mean no job was
            # taken. Normal at startup: compose starts the runner and the
            # gateway together, and the runner wins the race about half the
            # time. A transport failure *after* a claim falls through to the
            # handler below, which does not claim otherwise.
            print(
                f"agent runner: gateway unreachable, retrying: {exc}", file=sys.stderr, flush=True
            )
        except Exception as exc:
            # Keep serving: the store owns this job's outcome, and one bad
            # repository must not stop every other queued job.
            print(f"agent runner: job failed unexpectedly: {exc}", file=sys.stderr, flush=True)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        # Housekeeping on the idle tick: a snapshot cache on the same host as
        # every job worktree must not be the thing that fills the disk.
        cache = build_cache_from_env()
        if cache is not None:
            with contextlib.suppress(Exception):
                cache.purge_expired()

        # `run_once` returns 0 with nothing claimed too; sleeping only when the
        # queue was empty would need a separate signal, and a short sleep after
        # any job is harmless next to a job's own runtime.
        time.sleep(idle_sleep_s)


def _env_float(name: str, fallback: float) -> float:
    """Read a float setting, falling back rather than crash-looping on a typo."""
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        print(f"ignoring {name}={raw!r}: not a number", file=sys.stderr)
        return fallback


def default_worker_id() -> str:
    """Identify this runner, distinctly from its replicas.

    ``lease_owner`` is how an operator answers "which runner has this job" and
    "which one is stuck". Replicas share an environment, so a plain
    ``AGENT_WORKER_ID`` makes every one of them report the same name — the
    question stops being answerable exactly when a second replica makes it
    worth asking. The hostname is unique per container, so it is appended
    rather than replaced: the configured value still groups a fleet.

    Not a correctness fix — fencing is on ``(attempt_id, lease_generation)``,
    never on this string.
    """
    base = (os.environ.get("AGENT_WORKER_ID") or "runner").strip() or "runner"
    host = socket.gethostname().strip()
    return f"{base}-{host}" if host and not base.endswith(host) else base


def build_parser() -> argparse.ArgumentParser:
    """Build the runner CLI parser.

    Separate from :func:`main` so the deployment configuration can be checked
    against the flags that actually exist, rather than discovering a typo when
    a runner container crash-loops.
    """
    parser = argparse.ArgumentParser(description="Run queued agent jobs.")
    # Keep the deployment's old variable as a fallback until its hosts have
    # migrated. AGENT_GATEWAY_URL remains the public, preferred name.
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_GATEWAY_URL") or os.environ.get("FREEINFERENCE_BASE_URL", ""),
    )
    parser.add_argument("--worker-id", default=default_worker_id())
    parser.add_argument("--workdir", default=".")
    # Read from the environment the way --base-url and --workdir-root already
    # do. The compose overlay sets AGENT_LEASE_TTL and AGENT_TIMEOUT_S, and
    # neither reached the runner: every self-hosted job used the built-in
    # defaults regardless of what the operator configured, silently.
    parser.add_argument(
        "--lease-ttl",
        type=float,
        default=_env_float("AGENT_LEASE_TTL", DEFAULT_LEASE_TTL_S),
    )
    parser.add_argument(
        "--agent-timeout",
        type=float,
        default=_env_float("AGENT_TIMEOUT_S", DEFAULT_AGENT_TIMEOUT_S),
    )
    parser.add_argument("--generic-command", default=os.environ.get("AGENT_GENERIC_COMMAND"))
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep claiming jobs instead of exiting after one (self-hosted runner).",
    )
    parser.add_argument(
        "--workdir-root",
        default=os.environ.get("AGENT_WORKDIR_ROOT", "/var/lib/hybridinference/agent-jobs"),
        help="Where per-job worktrees are created in --loop mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by the runner workflow and the self-hosted service."""
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatcher_token = os.environ.get("AGENT_DISPATCHER_TOKEN", "")
    if not args.base_url or not dispatcher_token:
        parser.error("AGENT_GATEWAY_URL and AGENT_DISPATCHER_TOKEN are required")

    if args.loop:
        return run_forever(
            base_url=args.base_url,
            dispatcher_token=dispatcher_token,
            worker_id=args.worker_id,
            workdir_root=args.workdir_root,
            lease_ttl=args.lease_ttl,
            agent_timeout_s=args.agent_timeout,
            generic_command=args.generic_command,
        )

    return run_once(
        base_url=args.base_url,
        dispatcher_token=dispatcher_token,
        worker_id=args.worker_id,
        workdir=args.workdir,
        lease_ttl=args.lease_ttl,
        agent_timeout_s=args.agent_timeout,
        generic_command=args.generic_command,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ClaimedJob",
    "ControlPlane",
    "LeaseLost",
    "WorktreeError",
    "build_patch",
    "existing_checkout_sha",
    "main",
    "prepare_worktree",
    "run_once",
]
