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
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from serving.agent_jobs.runtimes import AgentRuntime, NormalizedEvent, get_runtime
from serving.agent_jobs.sandbox import SandboxBackend, SandboxSpec, build_backend_from_env

DEFAULT_LEASE_TTL_S = 120.0
HEARTBEAT_INTERVAL_S = 30.0
DEFAULT_AGENT_TIMEOUT_S = 3600.0


class LeaseLost(Exception):
    """Raised when this attempt no longer owns the job and must stop."""


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

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> ClaimedJob:
        """Build from the claim endpoint's response."""
        return cls(
            job_id=body["job_id"],
            attempt_id=body["attempt_id"],
            attempt_no=body["attempt_no"],
            repo=body["repo"],
            base_sha=body.get("base_sha"),
            task_prompt=body["task_prompt"],
            runtime=body["runtime"],
            model=body["model"],
            worker_token=body["worker_token"],
            # Older gateways return only worker_token; fall back so a
            # runner can still talk to one that predates scoped tokens.
            sandbox_token=body.get("sandbox_token") or body["worker_token"],
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

    def finish(self, state: str, detail: str | None = None) -> None:
        """Drive the fenced terminal transition."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/finish",
            {"state": state, "detail": detail},
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
    response = httpx.post(
        f"{base_url.rstrip('/')}/v1/agent/worker/claim",
        json={"worker_id": worker_id, "lease_ttl_seconds": lease_ttl},
        headers={"Authorization": f"Bearer {dispatcher_token}"},
        timeout=30.0,
    )
    response.raise_for_status()
    body = response.json()
    return ClaimedJob.from_response(body) if body else None


def build_patch(workdir: str) -> str:
    """Return the agent's work as a patch, or an empty string if it changed nothing.

    Uses ``git diff`` against the index plus untracked files staged with
    ``git add -N``, so new files appear in the diff. Nothing is committed and
    nothing is pushed: the trusted publisher owns that side.
    """
    subprocess.run(["git", "add", "-A", "-N"], cwd=workdir, check=False, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout


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
) -> tuple[int, str]:
    """Run the agent, streaming its output back as normalized events.

    Returns ``(exit_code, tail)``. Raises :class:`LeaseLost` if this attempt
    stops owning the job mid-run.
    """
    argv, extra_env = runtime.prepare(
        workdir=workdir,
        task_prompt=job.task_prompt,
        model=job.model,
        gateway_base_url=gateway_base_url,
        # Only the model-scoped credential crosses into the sandbox. The full
        # capability token — which can write events, artifacts and terminal
        # states — stays out here with the runner, so an agent that leaks its
        # credential can spend the job's capped budget and nothing more.
        credential=job.sandbox_token,
    )

    # Hermetic environment: only what a CLI genuinely needs, plus the runtime's
    # own variables. Inheriting the whole environment would hand the agent
    # whatever the host happens to be carrying.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
        if key in os.environ
    }
    env.update(extra_env)

    process = backend.spawn(SandboxSpec(argv=argv, workdir=workdir, env=env))

    deadline = time.monotonic() + timeout_s
    tail: list[str] = []
    for line in process.lines():
        tail.append(line)
        del tail[:-40]
        if heart.lease_lost:
            process.kill()
            raise LeaseLost("lease lost while the agent was running")
        if heart.cancel_requested:
            process.kill()
            control.append_event(NormalizedEvent("lifecycle", {"phase": "cancelled_by_owner"}))
            return 130, "".join(tail)
        if time.monotonic() > deadline:
            process.kill()
            control.append_event(
                NormalizedEvent("error", {"text": f"agent exceeded {timeout_s:.0f}s"})
            )
            return 124, "".join(tail)

        event = runtime.parse_event(line)
        if event is not None:
            control.append_event(event)

    exit_code = process.wait()
    stderr = process.stderr_text()
    if stderr.strip():
        tail.append(stderr[-2000:])
    return exit_code, "".join(tail)


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

    ``backend`` decides where the agent actually executes. It is checked
    before a job is claimed so a misconfigured host fails without first
    taking a job off the queue and burning one of its attempts.
    """
    backend = backend or build_backend_from_env()
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

    if shutil.which(runtime.binary) is None:
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
        exit_code, tail = run_agent(
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
            control.finish("cancelled", "cancelled by owner")
            return 0

        patch = build_patch(workdir)
        if patch.strip():
            control.save_artifact("patch", patch)
            control.append_event(
                NormalizedEvent("diff", {"bytes": len(patch.encode()), "stored": True})
            )
        else:
            control.append_event(NormalizedEvent("diff", {"bytes": 0, "stored": False}))

        if exit_code != 0:
            control.finish("failed", f"agent exited {exit_code}: {tail[-500:]}")
            return 1

        # The publisher runs outside the sandbox and drives publish/*; the
        # runner's job ends at a validated patch. Leaving the job `running`
        # would be a lie, so report success and let the publisher take it from
        # here when a patch exists.
        control.finish("succeeded", "agent completed" + ("" if patch.strip() else " (no changes)"))
        return 0
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
    backend.preflight()

    root = pathlib.Path(workdir_root)
    root.mkdir(parents=True, exist_ok=True)
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
        except Exception as exc:
            # Keep serving: the store owns this job's outcome, and one bad
            # repository must not stop every other queued job.
            print(f"agent runner: job failed unexpectedly: {exc}", file=sys.stderr, flush=True)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        # `run_once` returns 0 with nothing claimed too; sleeping only when the
        # queue was empty would need a separate signal, and a short sleep after
        # any job is harmless next to a job's own runtime.
        time.sleep(idle_sleep_s)


def build_parser() -> argparse.ArgumentParser:
    """Build the runner CLI parser.

    Separate from :func:`main` so the deployment configuration can be checked
    against the flags that actually exist, rather than discovering a typo when
    a runner container crash-loops.
    """
    parser = argparse.ArgumentParser(description="Run queued agent jobs.")
    parser.add_argument("--base-url", default=os.environ.get("FREEINFERENCE_BASE_URL", ""))
    parser.add_argument("--worker-id", default=os.environ.get("AGENT_WORKER_ID", "runner"))
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--lease-ttl", type=float, default=DEFAULT_LEASE_TTL_S)
    parser.add_argument("--agent-timeout", type=float, default=DEFAULT_AGENT_TIMEOUT_S)
    parser.add_argument("--generic-command", default=os.environ.get("AGENT_GENERIC_COMMAND"))
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep claiming jobs instead of exiting after one (self-hosted runner).",
    )
    parser.add_argument(
        "--workdir-root",
        default=os.environ.get("AGENT_WORKDIR_ROOT", "/var/lib/freeinference/agent-jobs"),
        help="Where per-job worktrees are created in --loop mode.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by the runner workflow and the self-hosted service."""
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatcher_token = os.environ.get("AGENT_DISPATCHER_TOKEN", "")
    if not args.base_url or not dispatcher_token:
        parser.error("FREEINFERENCE_BASE_URL and AGENT_DISPATCHER_TOKEN are required")

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


__all__ = ["ClaimedJob", "ControlPlane", "LeaseLost", "build_patch", "main", "run_once"]
