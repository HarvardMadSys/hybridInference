"""Pluggable sandbox backends — where one job's agent actually executes.

The runner is deliberately substrate-agnostic: it claims over HTTP, executes,
and reports over HTTP. *How* the execution is isolated is this module's job,
selected by configuration, so the same runner code serves dogfooding on CI,
self-hosted machines, and a multi-tenant fleet.

Two topologies exist and the difference is a security property, not a
deployment detail:

- **Runner inside the sandbox** (``process`` on an ephemeral CI VM): the VM is
  the isolation boundary and the runner shares it with the agent. Simple, and
  what the GitHub Actions dogfood uses.
- **Runner outside, sandbox per job** (``container`` / ``kata``): the runner is
  a long-lived trusted process that spawns an isolated sandbox per job. Only a
  model-scoped credential crosses into the sandbox; the full capability token
  that can write events, artifacts and terminal states stays outside. This is
  strictly better and is the target for anything multi-tenant.

Backends are intentionally thin: they take an argv the runtime adapter built
and hand back a line stream. They do not know what an agent is.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)

DEFAULT_IMAGE = "ghcr.io/harvardmadsys/freeinference-agent-sandbox:latest"
# containerd's Kata shim. Docker exposes VM-isolated runtimes under the same
# `--runtime` flag as runc, which is why one backend covers both: they are the
# same mechanism with a different isolation boundary underneath.
KATA_RUNTIME = "io.containerd.kata.v2"


class SandboxError(Exception):
    """Raised when a sandbox cannot be created or configured."""


@dataclass
class SandboxSpec:
    """Everything a backend needs to run one job's agent."""

    argv: list[str]
    workdir: str
    env: dict[str, str] = field(default_factory=dict)
    # Memory/CPU caps keep one runaway job from starving its neighbours on a
    # shared host. Ignored by the process backend, which has no boundary.
    memory_limit: str = "4g"
    cpu_limit: str = "2"


class SandboxProcess(ABC):
    """A running agent, exposing a line stream and a kill switch."""

    @abstractmethod
    def lines(self) -> Iterator[str]:
        """Yield the agent's stdout, line by line, as it is produced."""

    @abstractmethod
    def kill(self) -> None:
        """Terminate the agent immediately."""

    @abstractmethod
    def wait(self) -> int:
        """Wait for exit and return the status code."""

    @abstractmethod
    def stderr_text(self) -> str:
        """Return whatever the agent wrote to stderr."""


class SandboxBackend(ABC):
    """Creates isolated execution environments for agent jobs."""

    name: str = "abstract"

    @abstractmethod
    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Start the agent and return a handle to it."""

    def preflight(self) -> None:
        """Raise :class:`SandboxError` if this backend cannot run here.

        Called before a job is claimed so a misconfigured host fails loudly
        instead of taking a job off the queue and burning one of its attempts.
        Backends with nothing to check inherit this no-op.
        """
        return


# ── process: no isolation ──────────────────────────────────────────────


class _PopenProcess(SandboxProcess):
    """Wraps a plain ``subprocess.Popen``.

    stderr is drained on a background thread from the moment the process
    starts. Left unread, a chatty agent fills the pipe buffer and blocks
    forever on its next write — while the runner sits reading a stdout stream
    that will never produce another line.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._stderr_chunks: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        if process.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True, name="sandbox-stderr"
            )
            self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        """Read stderr to EOF, keeping a bounded tail."""
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr_chunks.append(line)
            # Bounded: a runaway agent must not turn its own noise into an
            # out-of-memory on the runner.
            del self._stderr_chunks[:-200]

    def lines(self) -> Iterator[str]:
        """Yield stdout lines from the child process."""
        assert self._process.stdout is not None
        yield from self._process.stdout

    def kill(self) -> None:
        """Kill the child process."""
        self._process.kill()

    def wait(self) -> int:
        """Wait for the child and return its exit status."""
        self._process.wait()
        return self._process.returncode

    def stderr_text(self) -> str:
        """Return the child's stderr tail, collected by the drain thread."""
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        return "".join(self._stderr_chunks)


class ProcessBackend(SandboxBackend):
    """Run the agent as a plain subprocess — **no isolation whatsoever**.

    Legitimate only where something *else* is the boundary: an ephemeral CI VM
    (the Actions dogfood) or a developer's own machine. On a shared host this
    gives the agent, and anything in the repository it executes, the runner's
    own privileges. :meth:`preflight` therefore refuses to start unless the
    operator has explicitly acknowledged that.
    """

    name = "process"

    def __init__(self, *, acknowledged_unsafe: bool = False) -> None:
        self._acknowledged = acknowledged_unsafe

    def preflight(self) -> None:
        """Refuse to run unisolated unless explicitly acknowledged."""
        if not self._acknowledged:
            raise SandboxError(
                "the 'process' sandbox backend provides no isolation: the agent "
                "runs with the runner's privileges. Set "
                "AGENT_SANDBOX_ALLOW_UNISOLATED=1 to accept that (only valid "
                "when an ephemeral VM or a trusted single-tenant host is the "
                "real boundary), or use AGENT_SANDBOX_BACKEND=container."
            )

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Start the agent directly on this host."""
        return _PopenProcess(
            subprocess.Popen(
                spec.argv,
                cwd=spec.workdir,
                env=spec.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        )


# ── container: docker / kata ───────────────────────────────────────────


class ContainerBackend(SandboxBackend):
    """Run the agent in a throwaway container, optionally VM-isolated.

    ``runtime=None`` gives an ordinary (shared-kernel) container, which is a
    reasonable boundary for repositories you already trust. ``runtime`` set to
    a VM-backed runtime such as :data:`KATA_RUNTIME` puts a kernel boundary
    around each job, which is what untrusted multi-tenant code needs — the
    agent installs dependencies and runs repository scripts, so a shared kernel
    means one kernel bug reaches every tenant.

    Networking is a policy input rather than a default: the sandbox must reach
    the gateway (models + event reporting) and, during setup, package
    registries — but nothing else. ``network`` names the docker network that
    encodes that policy for this deployment.
    """

    name = "container"

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        runtime: str | None = None,
        network: str = "bridge",
        docker_binary: str = "docker",
        extra_args: list[str] | None = None,
    ) -> None:
        self.image = image
        self.runtime = runtime
        self.network = network
        self.docker_binary = docker_binary
        self.extra_args = extra_args or []

    @property
    def is_vm_isolated(self) -> bool:
        """Whether each job gets its own kernel."""
        return self.runtime is not None

    def preflight(self) -> None:
        """Verify the container runtime is usable before accepting jobs."""
        try:
            result = subprocess.run(
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"{self.docker_binary} is not usable: {exc}") from exc
        if result.returncode != 0:
            raise SandboxError(f"{self.docker_binary} is not usable: {result.stderr.strip()[:200]}")
        if not self.is_vm_isolated:
            logger.warning(
                "agent_sandbox_shared_kernel",
                extra={
                    "event": "agent_sandbox_shared_kernel",
                    "detail": (
                        "container backend without a VM-isolated runtime: fine for "
                        "trusted repositories, not for untrusted multi-tenant code"
                    ),
                },
            )

    def build_command(self, spec: SandboxSpec) -> list[str]:
        """Compose the ``docker run`` argv for one job.

        Kept separate from :meth:`spawn` so the isolation flags are directly
        testable without a container runtime installed.
        """
        argv = [
            self.docker_binary,
            "run",
            "--rm",
            "--interactive",
            # The sandbox is thrown away after the job, so nothing it writes
            # outside the mounted worktree can persist or leak to a neighbour.
            "--mount",
            f"type=bind,source={spec.workdir},target=/workspace",
            "--workdir",
            "/workspace",
            "--network",
            self.network,
            "--memory",
            spec.memory_limit,
            "--cpus",
            spec.cpu_limit,
            # Defence in depth for the shared-kernel case; harmless under Kata.
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
        ]
        if self.runtime:
            argv += ["--runtime", self.runtime]
        for key, value in sorted(spec.env.items()):
            argv += ["--env", f"{key}={value}"]
        argv += self.extra_args
        argv.append(self.image)
        argv += spec.argv
        return argv

    def spawn(self, spec: SandboxSpec) -> SandboxProcess:
        """Start the agent inside a throwaway container."""
        command = self.build_command(spec)
        logger.info(
            "agent_sandbox_spawn",
            extra={
                "event": "agent_sandbox_spawn",
                "backend": self.name,
                "vm_isolated": self.is_vm_isolated,
                "image": self.image,
            },
        )
        return _PopenProcess(
            subprocess.Popen(
                command,
                # The docker client itself needs a real environment (DOCKER_HOST,
                # certs); the agent's environment is passed via --env above and
                # does not leak into the client's.
                env={**os.environ},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        )


# ── selection ──────────────────────────────────────────────────────────


def build_backend_from_env(env: dict[str, str] | None = None) -> SandboxBackend:
    """Construct the configured backend.

    ``AGENT_SANDBOX_BACKEND`` is ``process`` (default, CI/dev), ``container``
    (shared kernel) or ``kata`` (VM-isolated container). The remaining
    variables tune the container backends: ``AGENT_SANDBOX_IMAGE``,
    ``AGENT_SANDBOX_NETWORK``, ``AGENT_SANDBOX_RUNTIME`` (overrides the Kata
    shim name), ``AGENT_SANDBOX_DOCKER`` and ``AGENT_SANDBOX_EXTRA_ARGS``.
    """
    source = env if env is not None else dict(os.environ)
    choice = (source.get("AGENT_SANDBOX_BACKEND") or "process").strip().lower()

    if choice == "process":
        return ProcessBackend(
            acknowledged_unsafe=source.get("AGENT_SANDBOX_ALLOW_UNISOLATED", "") not in ("", "0")
        )
    if choice in ("container", "docker", "kata"):
        runtime = source.get("AGENT_SANDBOX_RUNTIME") or (
            KATA_RUNTIME if choice == "kata" else None
        )
        return ContainerBackend(
            image=source.get("AGENT_SANDBOX_IMAGE") or DEFAULT_IMAGE,
            runtime=runtime,
            network=source.get("AGENT_SANDBOX_NETWORK") or "bridge",
            docker_binary=source.get("AGENT_SANDBOX_DOCKER") or "docker",
            extra_args=shlex.split(source.get("AGENT_SANDBOX_EXTRA_ARGS") or ""),
        )
    raise SandboxError(
        f"unknown AGENT_SANDBOX_BACKEND {choice!r}; expected process, container or kata"
    )
