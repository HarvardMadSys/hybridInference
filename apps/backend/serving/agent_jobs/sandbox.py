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

import contextlib
import os
import shlex
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from serving.agent_jobs.egress import (
    PHASES,
    EgressPolicy,
    EgressPolicyError,
    EgressTier,
    build_policy_from_env,
)
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)

DEFAULT_IMAGE = "ghcr.io/harvardmadsys/freeinference-agent-sandbox:latest"
# containerd's Kata shim. Docker exposes VM-isolated runtimes under the same
# `--runtime` flag as runc, which is why one backend covers both: they are the
# same mechanism with a different isolation boundary underneath.
KATA_RUNTIME = "io.containerd.kata.v2"

# Must match `useradd --uid` in deploy/docker/Dockerfile.agent-sandbox. The
# runner chowns each job worktree to this id before mounting it, so a drift
# between the two means the agent cannot write to its own working tree.
SANDBOX_UID = 10001
SANDBOX_GID = 10001
SANDBOX_HOME = "/home/agent"
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class SandboxError(Exception):
    """Raised when a sandbox cannot be created or configured."""


def _walk_entries(path: str) -> Iterator[tuple[str, bool]]:
    """Yield ``(path, is_dir)`` for the root and everything beneath it.

    Symlinks are yielded but never followed, so nothing outside the worktree is
    reached by an operation applied to the tree.
    """
    yield path, True
    for root, dirs, files in os.walk(path):
        for name in dirs:
            yield os.path.join(root, name), True
        for name in files:
            yield os.path.join(root, name), False


@dataclass
class SandboxSpec:
    """Everything a backend needs to run one job's agent."""

    argv: list[str]
    workdir: str
    env: dict[str, str] = field(default_factory=dict)
    # Which lifecycle phase this spawn belongs to. The two get different egress
    # tiers: setup installs dependencies and needs a registry, the agent turn
    # afterwards does not — and the agent turn is the one running untrusted
    # model output, so it is the one that gets the least reach.
    phase: str = "agent"
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

    def preflight(self, workdir_root: str | None = None) -> None:
        """Raise :class:`SandboxError` if this backend cannot run here.

        Called before a job is claimed so a misconfigured host fails loudly
        instead of taking a job off the queue and burning one of its attempts.
        Backends with nothing to check inherit this no-op.
        """
        return

    def has_binary(self, name: str) -> bool:
        """Whether ``name`` will be runnable inside the sandbox.

        Only the backend can answer this. Checking the *runner's* PATH is the
        wrong question for every isolated backend: the agent CLIs live in the
        sandbox image, so a runner that looked for them locally would refuse
        every job on a correctly configured host.
        """
        return True

    def base_env(self) -> dict[str, str]:
        """The environment a process starts with inside this sandbox.

        The runner must not forward its own ``PATH``/``HOME`` into an isolated
        sandbox: those name paths in the *runner's* filesystem. ``HOME`` is the
        one that bites — an agent CLI writes its config there, and pointing it
        at a directory that does not exist (or is not writable by the sandbox
        user) fails the job for a reason nothing in the logs explains.
        """
        return {}

    def adopt_workdir(self, path: str) -> None:
        """Hand ``path`` and everything under it to the sandbox's user.

        Called after the runner has populated the worktree and before the agent
        runs. A no-op where the agent runs as the runner's own user.
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

    def preflight(self, workdir_root: str | None = None) -> None:
        """Refuse to run unisolated unless explicitly acknowledged."""
        if not self._acknowledged:
            raise SandboxError(
                "the 'process' sandbox backend provides no isolation: the agent "
                "runs as the runner's own user, which means it can read the "
                "dispatcher credential out of /proc and claim other tenants' "
                "jobs. Set AGENT_SANDBOX_ALLOW_UNISOLATED=1 to accept that "
                "(only valid single-tenant, where an ephemeral VM is the real "
                "boundary), or use AGENT_SANDBOX_BACKEND=container."
            )

    def has_binary(self, name: str) -> bool:
        """The agent runs on this host, so this host must have the binary."""
        return shutil.which(name) is not None

    def base_env(self) -> dict[str, str]:
        """Inherit the host's essentials — the agent runs on this host."""
        return {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
            if key in os.environ
        }

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
        allow_open_network: bool = False,
        egress: EgressPolicy | None = None,
        uid: int = SANDBOX_UID,
        gid: int = SANDBOX_GID,
        home: str = SANDBOX_HOME,
    ) -> None:
        self.image = image
        self.runtime = runtime
        self.network = network
        self.docker_binary = docker_binary
        self.extra_args = extra_args or []
        self.allow_open_network = allow_open_network
        self.egress = egress
        self.uid = uid
        self.gid = gid
        self.home = home

    @property
    def is_vm_isolated(self) -> bool:
        """Whether each job gets its own kernel."""
        return self.runtime is not None

    def network_for_phase(self, phase: str) -> str:
        """Resolve the docker network this phase runs on.

        With no egress policy configured this is the single network the backend
        was built with, so an existing deployment behaves exactly as before.
        """
        if self.egress is None:
            return self.network
        try:
            return self.egress.network_for(phase)
        except EgressPolicyError:
            # A phase with no network configured for its tier must not silently
            # fall back to a more open one.
            raise

    def base_env(self) -> dict[str, str]:
        """The image's own environment, not the runner's.

        ``HOME`` must name a directory that exists in the *image* and is
        writable by the sandbox user: every agent CLI writes state there, and
        inheriting the runner's ``HOME=/root`` makes the CLI fail on startup
        with an error about a path the operator has never heard of.
        """
        return {"HOME": self.home, "PATH": SANDBOX_PATH, "LANG": "C.UTF-8"}

    def adopt_workdir(self, path: str) -> None:
        """Give the job worktree to the sandbox user before the agent runs.

        The runner creates and populates this tree as itself (root, in the
        Compose deployment) while the sandbox runs unprivileged. Without this
        the agent cannot write a single file — and git refuses to operate at
        all, because a repository owned by another user is "dubious ownership".
        """
        try:
            for target, _is_dir in _walk_entries(path):
                # lchown, not chown: a symlink in the tree must not be followed
                # to whatever it points at outside the worktree.
                with contextlib.suppress(FileNotFoundError):
                    os.lchown(target, self.uid, self.gid)
        except PermissionError:
            # An unprivileged runner cannot give the tree away. World-writable
            # is the only remaining way for the sandbox user to work, and it is
            # only defensible on a dedicated single-purpose host.
            #
            # It has to be the whole tree. Opening up only the root leaves every
            # checked-out file at its original mode and owner, so the agent can
            # create new files and cannot edit any existing one — which is most
            # of what an agent does. That fallback reads as working and is not.
            self._make_world_writable(path)
            logger.warning(
                "agent_sandbox_workdir_world_writable",
                extra={
                    "event": "agent_sandbox_workdir_world_writable",
                    "detail": (
                        "the runner is not root and cannot chown the job worktree to "
                        f"uid {self.uid}; the tree was made world-writable instead. "
                        "Run the runner as root, or accept that any local user can "
                        "read and modify job worktrees on this host — including "
                        "planting code the agent will then execute."
                    ),
                },
            )

    @staticmethod
    def _make_world_writable(path: str) -> None:
        """Grant everyone read/write on the tree, and traverse on directories."""
        for target, is_dir in _walk_entries(path):
            # chmod follows symlinks and there is no lchmod on Linux, so a link
            # in the worktree would otherwise let the agent widen permissions
            # on a file outside it.
            if os.path.islink(target):
                continue
            try:
                mode = os.stat(target).st_mode & 0o7777
                # `a+rwX`: the execute bit only where it already means something
                # (directories, and files that were already executable), so this
                # does not turn every data file into a program.
                mode |= 0o666 | (0o111 if is_dir or mode & 0o111 else 0)
                os.chmod(target, mode)
            except OSError:
                continue

    def preflight(self, workdir_root: str | None = None) -> None:
        """Verify the container runtime is usable before accepting jobs.

        ``workdir_root``, when given, is test-mounted: a path the daemon cannot
        bind (a macOS temp dir under colima, a directory outside the VM's
        shared mounts) otherwise fails every single job with an opaque exit 125
        instead of failing once at startup.
        """
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
        self._check_networks_are_closed()
        if workdir_root is not None:
            self._check_bind_mountable(workdir_root)
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

    def _check_networks_are_closed(self) -> None:
        """Check every network a phase can run on, not just the default one.

        A per-phase policy means there is no single network to validate: a
        deployment can be closed for the agent turn and open for setup, and
        only the tier names say which is which.
        """
        if self.egress is None:
            self._check_one_network(self.network)
            return
        for phase in PHASES:
            tier = self.egress.tier_for(phase)
            try:
                network = self.egress.network_for(phase)
            except EgressPolicyError:
                # A phase whose tier names no network cannot run. That is only
                # fatal for a phase this runner actually spawns — the setup
                # phase does not exist yet, and refusing to start over a
                # capability nothing uses would be a startup failure with no
                # cause an operator could act on. It still fails loudly at
                # spawn if a phase is ever added without configuring it.
                if phase == "agent":
                    raise
                continue
            # Trusted and custom tiers are allowlist-fronted by construction;
            # asserting `internal` on them would be wrong. Full is already
            # gated behind the acknowledgement in build_policy_from_env.
            if tier is EgressTier.PLATFORM_ONLY:
                self._check_one_network(network)

    def _check_one_network(self, network: str) -> None:
        """Refuse to start unless the sandbox network denies egress by default.

        The design puts egress control at the network layer and says
        degradation must fail closed. An unset or open network is therefore a
        startup failure, not a warning: the agent runs untrusted repository
        code, and "nobody configured this" must not be the same thing as "full
        internet".
        """
        if self.allow_open_network:
            logger.warning(
                "agent_sandbox_open_network",
                extra={
                    "event": "agent_sandbox_open_network",
                    "detail": (
                        f"sandbox network {network!r} is not internal and the "
                        "operator has accepted that; the agent can reach the internet"
                    ),
                },
            )
            return
        if not network:
            raise SandboxError(
                "AGENT_SANDBOX_NETWORK is not set. There is deliberately no default: "
                "an unset value used to mean the Docker bridge, i.e. unrestricted "
                "outbound internet for untrusted repository code. Point it at an "
                "internal network (the compose overlay declares `agent-egress`), or "
                "set AGENT_SANDBOX_ALLOW_OPEN_NETWORK=1 to accept open egress."
            )
        probe = subprocess.run(
            [self.docker_binary, "network", "inspect", network, "--format", "{{.Internal}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0:
            raise SandboxError(
                f"the sandbox network {network!r} does not exist: "
                f"{probe.stderr.strip()[:200]}. Every job would fail at spawn."
            )
        if probe.stdout.strip().lower() != "true":
            raise SandboxError(
                f"the sandbox network {network!r} is not `internal`, so the agent "
                "would have unrestricted egress. Declare it `internal: true` (as the "
                "compose overlay does), front it with an allowlist proxy, or set "
                "AGENT_SANDBOX_ALLOW_OPEN_NETWORK=1 to accept that deliberately."
            )

    def check_gateway_reachable(self, base_url: str) -> None:
        """Fail startup if the sandbox cannot reach the gateway it will be given.

        `platform_only` means "our gateway and nothing else", which quietly
        assumes the gateway is *on that network* — true when it is the compose
        `backend` service, false the moment an operator points
        AGENT_GATEWAY_URL at a remote one. The sandbox then resolves nothing,
        every job dies at its first model call, and the error reads like a
        broken model rather than a network that was never going to work.

        Probed rather than inferred: one container on the phase's real network,
        asking whether the host resolves. A heuristic on the URL would be wrong
        for every deployment that does route out.
        """
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").strip()
        if not host:
            return
        network = self.network_for_phase("agent")
        probe = subprocess.run(
            [
                self.docker_binary,
                "run",
                "--rm",
                "--network",
                network,
                "--entrypoint",
                "/bin/sh",
                self.image,
                "-c",
                f"getent hosts {shlex.quote(host)} >/dev/null 2>&1",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode != 0:
            raise SandboxError(
                f"the sandbox network {network!r} cannot resolve {host!r}, the gateway "
                "the agent is told to call. Every job would fail at its first model "
                "call. A closed `platform_only` network only works when the gateway is "
                "on it (the compose `backend` service); for a remote gateway use a "
                "network that routes to it (AGENT_EGRESS_AGENT_TIER=custom with "
                "AGENT_EGRESS_NETWORK_CUSTOM)."
            )

    def _check_bind_mountable(self, workdir_root: str) -> None:
        """Fail startup if the daemon cannot bind-mount the job workdir root."""
        # Under the runtime jobs will actually use. Without `--runtime` the
        # probe ran under the daemon's default (runc) while the shipped default
        # backend is kata — so it passed on a host with no Kata shim, and every
        # job then died at spawn, which is exactly what this check exists to
        # turn into one startup failure.
        argv = [self.docker_binary, "run", "--rm"]
        if self.runtime:
            argv += ["--runtime", self.runtime]
        argv += [
            "--mount",
            f"type=bind,source={workdir_root},target=/probe",
            "--user",
            f"{self.uid}:{self.gid}",
            "--entrypoint",
            "true",
            self.image,
        ]
        probe = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if probe.returncode != 0:
            raise SandboxError(
                f"the container runtime {self.runtime or 'default'} could not start a "
                f"container mounting {workdir_root!r}: "
                f"{probe.stderr.strip()[:200]}. Job worktrees must live on a path "
                "the daemon can see (inside the VM for colima/Lima, or a shared "
                "mount) — otherwise every job fails at spawn."
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
            # Stated rather than inherited from the image: the runner chowns
            # the worktree to exactly this id, and a silent drift between the
            # two leaves the agent unable to write to its own working tree.
            "--user",
            f"{self.uid}:{self.gid}",
            "--network",
            self.network_for_phase(spec.phase),
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
    shim name), ``AGENT_SANDBOX_DOCKER``, ``AGENT_SANDBOX_EXTRA_ARGS`` and
    ``AGENT_SANDBOX_UID``/``AGENT_SANDBOX_GID``/``AGENT_SANDBOX_HOME`` for a
    custom sandbox image whose user differs from the one we ship.
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
            # No fallback. `bridge` meant an unset variable gave the agent full
            # outbound internet — a missing setting opening the boundary rather
            # than closing it, which is the opposite of the fail-closed rule the
            # process backend already follows. Preflight refuses if it is unset.
            network=source.get("AGENT_SANDBOX_NETWORK") or "",
            docker_binary=source.get("AGENT_SANDBOX_DOCKER") or "docker",
            extra_args=shlex.split(source.get("AGENT_SANDBOX_EXTRA_ARGS") or ""),
            allow_open_network=source.get("AGENT_SANDBOX_ALLOW_OPEN_NETWORK", "") not in ("", "0"),
            egress=build_policy_from_env(source),
            uid=int(source.get("AGENT_SANDBOX_UID") or SANDBOX_UID),
            gid=int(source.get("AGENT_SANDBOX_GID") or SANDBOX_GID),
            home=source.get("AGENT_SANDBOX_HOME") or SANDBOX_HOME,
        )
    raise SandboxError(
        f"unknown AGENT_SANDBOX_BACKEND {choice!r}; expected process, container or kata"
    )
