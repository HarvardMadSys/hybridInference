"""Unit tests for pluggable sandbox backends.

The isolation flags are the security surface here, so they are asserted on the
composed command rather than trusted to a container runtime being installed.
"""

from __future__ import annotations

import signal
import subprocess
from types import SimpleNamespace

import pytest

from serving.agent_jobs.egress import EgressPolicyError
from serving.agent_jobs.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    ProcessBackend,
    SandboxError,
    SandboxSpec,
    _ContainerTerminalProcess,
    build_backend_from_env,
)

_SPEC = SandboxSpec(argv=["claude", "-p", "hi"], workdir="/tmp/wd", env={"A": "1"})


def test_process_backend_refuses_to_run_unisolated_by_default():
    """Running with no isolation must be an explicit operator decision."""
    with pytest.raises(SandboxError) as excinfo:
        ProcessBackend().preflight()
    assert "no isolation" in str(excinfo.value)


def test_process_backend_runs_when_acknowledged():
    """An operator who accepts the risk (ephemeral VM) may proceed."""
    ProcessBackend(acknowledged_unsafe=True).preflight()


def test_process_backend_executes_and_streams():
    """The process backend really runs a command and yields its output."""
    backend = ProcessBackend(acknowledged_unsafe=True)
    proc = backend.spawn(
        SandboxSpec(argv=["/bin/sh", "-c", "echo one; echo two"], workdir="/tmp", env={})
    )
    lines = [line.strip() for line in proc.lines()]
    assert proc.wait() == 0
    assert lines == ["one", "two"]


def test_process_backend_refuses_persistent_terminals_even_when_acknowledged():
    """A local PTY cannot reliably kill job-control groups across macOS/Linux."""
    backend = ProcessBackend(acknowledged_unsafe=True)
    with pytest.raises(SandboxError, match="container or kata"):
        backend.spawn_terminal(_SPEC, rows=24, cols=80)


def test_container_command_carries_the_isolation_flags():
    """The composed docker command pins the boundary we claim to have."""
    command = ContainerBackend(image="img:1", network="agent-net").build_command(_SPEC)
    joined = " ".join(command)

    assert command[:3] == ["docker", "run", "--rm"]
    # Throwaway, bind-mounted worktree only.
    assert "type=bind,source=/tmp/wd,target=/workspace" in joined
    assert "--workdir /workspace" in joined
    # Privilege and resource containment.
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined
    assert "--memory 4g" in joined
    # Network is policy, not a default-open.
    assert "--network agent-net" in joined
    # The agent's argv comes after the image, never before it.
    assert command[command.index("img:1") + 1 :] == ["claude", "-p", "hi"]


def test_container_terminal_keeps_isolation_and_adds_a_named_tty():
    """Terminal management must not weaken the ordinary sandbox boundary."""
    backend = ContainerBackend(image="img:1", network="agent-net")
    command = backend.build_terminal_command(_SPEC, name="hyi-terminal-test")
    joined = " ".join(command)

    assert command[:4] == ["docker", "run", "--rm", "--interactive"]
    assert "--tty" in command
    assert command[command.index("--name") + 1] == "hyi-terminal-test"
    assert command.index("--name") < command.index("img:1")
    assert "type=bind,source=/tmp/wd,target=/workspace" in joined
    assert "--user 10001:10001" in joined
    assert "--network agent-net" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined
    assert "--label org.hybridinference.agent-terminal=true" in joined
    assert "org.hybridinference.agent-terminal-owner=" in joined
    assert command[command.index("img:1") + 1 :] == ["claude", "-p", "hi"]


def test_container_terminal_attaches_docker_client_to_a_real_tty(tmp_path):
    """Docker rejects ``--tty`` unless its own stdin is a terminal."""
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "container" ]; then exit 0; fi\n'
        'if [ ! -t 0 ]; then printf "the input device is not a TTY\\n" >&2; exit 1; fi\n'
        'printf "tty-ready\\n"\n'
        'printf "size:%s\\n" "$(stty size)"\n'
        "IFS= read -r value\n"
        'printf "received:%s\\n" "$value"\n'
    )
    fake_docker.chmod(0o700)
    backend = ContainerBackend(image="img:1", docker_binary=str(fake_docker))

    terminal = backend.spawn_terminal(_SPEC, rows=31, cols=101)
    terminal.write(b"ping\n")
    output = b"".join(terminal.chunks())

    assert terminal.wait() == 0
    assert b"tty-ready" in output
    assert b"size:31 101" in output
    assert b"received:ping" in output


@pytest.mark.parametrize(
    "failure", [OSError("docker unavailable"), subprocess.TimeoutExpired([], 15)]
)
def test_container_terminal_resize_wraps_process_failures(monkeypatch, failure):
    """Docker invocation failures stay inside the sandbox error contract."""

    class _AttachedClient:
        pid = 1234
        stdout = None

        @staticmethod
        def poll():
            return None

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("serving.agent_jobs.sandbox.subprocess.run", fail)
    terminal = _ContainerTerminalProcess(
        _AttachedClient(),
        docker_binary="docker",
        container_name="hyi-terminal-test",
        input_fd=None,
    )

    with pytest.raises(SandboxError, match="cannot be resized"):
        terminal.resize(30, 100)


def test_container_terminal_kill_always_kills_client_and_force_removes(monkeypatch):
    """A timed-out Docker kill cannot skip either fallback cleanup step."""
    calls: list[list[str]] = []
    killed_groups: list[tuple[int, int]] = []

    class _AttachedClient:
        pid = 4321
        stdout = None
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self):
            self.returncode = -signal.SIGKILL

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "kill":
            raise subprocess.TimeoutExpired(argv, 15)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("serving.agent_jobs.sandbox.subprocess.run", fake_run)
    monkeypatch.setattr(
        "serving.agent_jobs.sandbox.os.killpg",
        lambda pid, sig: killed_groups.append((pid, sig)),
    )
    terminal = _ContainerTerminalProcess(
        _AttachedClient(),
        docker_binary="docker",
        container_name="hyi-terminal-test",
        input_fd=None,
    )

    terminal.kill()
    terminal.kill()

    assert calls == [
        ["docker", "kill", "hyi-terminal-test"],
        ["docker", "rm", "--force", "hyi-terminal-test"],
    ]
    assert killed_groups == [(4321, signal.SIGKILL)]


def test_container_terminal_kill_failure_can_be_retried(monkeypatch):
    """Cleanup is not marked complete until force-removal is confirmed."""
    calls: list[list[str]] = []
    remove_attempts = 0

    class _AttachedClient:
        pid = 4321
        stdout = None
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self):
            self.returncode = -signal.SIGKILL

    def fake_run(argv, **kwargs):
        nonlocal remove_attempts
        calls.append(argv)
        if argv[1] == "rm":
            remove_attempts += 1
            if remove_attempts == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("serving.agent_jobs.sandbox.subprocess.run", fake_run)
    monkeypatch.setattr("serving.agent_jobs.sandbox.os.killpg", lambda *_args: None)
    terminal = _ContainerTerminalProcess(
        _AttachedClient(),
        docker_binary="docker",
        container_name="hyi-terminal-test",
        input_fd=None,
    )

    with pytest.raises(SandboxError, match="cleanup could not be confirmed"):
        terminal.kill()
    terminal.kill()
    terminal.kill()

    assert calls == [
        ["docker", "kill", "hyi-terminal-test"],
        ["docker", "rm", "--force", "hyi-terminal-test"],
        ["docker", "kill", "hyi-terminal-test"],
        ["docker", "rm", "--force", "hyi-terminal-test"],
    ]


def test_terminal_broker_cleanup_is_stable_and_scoped_to_one_workdir(monkeypatch, tmp_path):
    """Restart cleanup targets only containers owned by this workdir root."""
    calls: list[list[str]] = []
    results = [
        SimpleNamespace(returncode=0, stdout="dead1\ndead2\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return results.pop(0)

    monkeypatch.setattr("serving.agent_jobs.sandbox.subprocess.run", fake_run)
    backend = ContainerBackend(image="img:1", network="agent-net")
    backend.prepare_terminal_broker(str(tmp_path))
    command = backend.build_terminal_command(_SPEC, name="hyi-terminal-test")
    owner_label = next(
        value for value in command if value.startswith("org.hybridinference.agent-terminal-owner=")
    )
    restarted = ContainerBackend(image="img:1", network="agent-net")
    restarted.prepare_terminal_broker(str(tmp_path))
    restarted_command = restarted.build_terminal_command(_SPEC, name="hyi-terminal-test-2")
    restarted_owner_label = next(
        value
        for value in restarted_command
        if value.startswith("org.hybridinference.agent-terminal-owner=")
    )

    assert calls[0][:5] == ["docker", "container", "ls", "--all", "--quiet"]
    assert calls[0][-4:] == [
        "--filter",
        "label=org.hybridinference.agent-terminal=true",
        "--filter",
        f"label={owner_label}",
    ]
    assert calls[1] == ["docker", "rm", "--force", "dead1", "dead2"]
    assert owner_label.startswith("org.hybridinference.agent-terminal-owner=workdir-")
    assert restarted_owner_label == owner_label


def test_plain_container_is_not_vm_isolated():
    """A shared-kernel container must not claim VM isolation."""
    backend = ContainerBackend(image="img:1")
    assert backend.is_vm_isolated is False
    assert "--runtime" not in backend.build_command(_SPEC)


def test_kata_backend_requests_the_vm_runtime():
    """Selecting kata puts a kernel boundary around each job."""
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "kata",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_SANDBOX_NETWORK": "agent-egress",
        }
    )
    assert isinstance(backend, ContainerBackend)
    assert backend.is_vm_isolated is True
    command = backend.build_command(_SPEC)
    assert command[command.index("--runtime") + 1] == KATA_RUNTIME


def test_env_reaches_the_container_as_env_flags_not_inherited():
    """Agent environment is passed explicitly, never inherited by accident."""
    command = ContainerBackend(image="img:1").build_command(
        SandboxSpec(argv=["x"], workdir="/w", env={"TOKEN": "secret", "B": "2"})
    )
    assert "TOKEN=secret" in command
    assert command.count("--env") == 2


def test_backend_selection_from_env():
    """The three configured shapes resolve to the right backend."""
    assert isinstance(build_backend_from_env({}), ProcessBackend)
    assert isinstance(
        build_backend_from_env(
            {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"}
        ),
        ContainerBackend,
    )
    with pytest.raises(SandboxError):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "nonsense"})


def test_explicit_runtime_override_wins():
    """An operator can name a different VM runtime than the Kata default."""
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_RUNTIME": "runsc",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_SANDBOX_NETWORK": "agent-egress",
        }
    )
    assert backend.is_vm_isolated is True
    assert backend.build_command(_SPEC)[backend.build_command(_SPEC).index("--runtime") + 1] == (
        "runsc"
    )


def test_a_container_backend_without_an_image_refuses() -> None:
    """No default, because an unqualified name is not inert.

    `docker run hybridinference/agent-sandbox` resolves through Docker Hub, so
    a default here hands the container an untrusted agent runs inside to
    whoever registered that namespace.
    """
    with pytest.raises(ValueError, match="AGENT_SANDBOX_IMAGE"):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "container"})
    with pytest.raises(ValueError, match="AGENT_SANDBOX_IMAGE"):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "kata", "AGENT_SANDBOX_IMAGE": "  "})


def test_runtime_flag_is_positioned_where_docker_reads_it():
    """`--runtime` must precede the image, or docker treats it as an argument.

    A dropped or misplaced flag is the dangerous failure mode: the kata backend
    would silently run as a plain shared-kernel container while every log line
    and config claimed VM isolation. Verified against a real daemon separately
    (an unknown runtime is refused rather than ignored); this pins the argv
    position so a refactor cannot quietly break it.
    """
    from serving.agent_jobs.sandbox import KATA_RUNTIME

    command = ContainerBackend(image="img:1", runtime=KATA_RUNTIME).build_command(_SPEC)
    runtime_index = command.index("--runtime")
    image_index = command.index("img:1")
    assert command[runtime_index + 1] == KATA_RUNTIME
    assert runtime_index < image_index, "docker only reads --runtime before the image"


# ── who runs, and in what environment ──────────────────────────────────
#
# Three separate bugs lived here, and all three shared a shape: the runner
# answering a question only the backend can answer. It checked its own PATH for
# the agent CLI (which lives in the sandbox image), forwarded its own HOME into
# the container (which does not exist there), and left job worktrees owned by
# root (which the unprivileged sandbox user cannot write).


def test_the_container_backend_does_not_look_for_agent_binaries_on_the_runner():
    """The CLIs live in the sandbox image, not on the host that starts it.

    Probing the runner's PATH here is what made a correctly configured host
    refuse every job with "runtime binary 'claude' is not installed".
    """
    backend = ContainerBackend(image="img")
    assert backend.has_binary("claude") is True
    assert backend.has_binary("definitely-not-installed-anywhere") is True


def test_the_process_backend_does_check_this_host():
    """With no isolation the agent runs here, so here is where it must exist."""
    backend = ProcessBackend(acknowledged_unsafe=True)
    assert backend.has_binary("sh") is True
    assert backend.has_binary("definitely-not-installed-anywhere") is False


def test_the_container_env_is_the_image_s_not_the_runner_s(monkeypatch):
    """HOME must name a directory inside the image, writable by the sandbox user.

    Forwarding the runner's HOME (``/root`` in the Compose deployment) makes
    every agent CLI fail on startup trying to write its config, with an error
    naming a path that does not exist in the container.
    """
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("PATH", "/app/.venv/bin:/usr/bin")

    env = ContainerBackend(image="img").base_env()

    assert env["HOME"] == "/home/agent"
    assert "/app/.venv/bin" not in env["PATH"]


def test_the_process_env_does_inherit_the_host(monkeypatch):
    """The unisolated backend runs here, so the host's environment is correct."""
    monkeypatch.setenv("HOME", "/home/somebody")
    assert ProcessBackend(acknowledged_unsafe=True).base_env()["HOME"] == "/home/somebody"


def test_the_container_runs_as_the_uid_the_runner_chowns_to():
    """A drift between these two leaves the agent unable to write anything."""
    from serving.agent_jobs.sandbox import SANDBOX_GID, SANDBOX_UID

    command = ContainerBackend(image="img").build_command(_SPEC)

    assert "--user" in command
    assert command[command.index("--user") + 1] == f"{SANDBOX_UID}:{SANDBOX_GID}"


def test_adopting_a_workdir_falls_back_to_world_writable_off_root(tmp_path, monkeypatch):
    """An unprivileged runner cannot chown, and must not fail the job silently."""

    def refuse(*args, **kwargs):
        raise PermissionError("not root")

    monkeypatch.setattr("os.chown", refuse)
    (tmp_path / "file.txt").write_text("x")

    ContainerBackend(image="img").adopt_workdir(str(tmp_path))

    assert tmp_path.stat().st_mode & 0o777 == 0o777


def test_adopting_a_workdir_is_a_no_op_without_isolation(tmp_path):
    """The process backend runs as the runner; there is nobody to hand it to."""
    before = tmp_path.stat().st_mode
    ProcessBackend(acknowledged_unsafe=True).adopt_workdir(str(tmp_path))
    assert tmp_path.stat().st_mode == before


def test_preflight_accepts_the_workdir_root_on_every_backend():
    """The runner passes it unconditionally; a backend that cannot take it crashes.

    This was previously papered over with a try/except TypeError in the runner,
    which would also have swallowed a genuine TypeError from inside preflight.
    """
    ProcessBackend(acknowledged_unsafe=True).preflight(workdir_root="/tmp")


def test_the_world_writable_fallback_reaches_the_whole_tree(tmp_path, monkeypatch):
    """Opening up only the root leaves every checked-out file unwritable.

    The agent could create new files and edit none of them — which is most of
    what an agent does. The fallback then reads as working and is not.
    """

    def refuse(*args, **kwargs):
        raise PermissionError("not root")

    monkeypatch.setattr("os.chown", refuse)
    monkeypatch.setattr("os.lchown", refuse)
    nested = tmp_path / "apps" / "backend"
    nested.mkdir(parents=True)
    source = nested / "main.py"
    source.write_text("x")
    source.chmod(0o644)
    nested.chmod(0o755)

    ContainerBackend(image="img").adopt_workdir(str(tmp_path))

    assert source.stat().st_mode & 0o002, "an existing file must become writable"
    assert nested.stat().st_mode & 0o002, "a nested directory must become writable"
    assert nested.stat().st_mode & 0o001, "a directory must stay traversable"


def test_the_fallback_does_not_widen_permissions_outside_the_worktree(tmp_path, monkeypatch):
    """chmod follows symlinks and Linux has no lchmod, so links must be skipped."""

    def refuse(*args, **kwargs):
        raise PermissionError("not root")

    monkeypatch.setattr("os.chown", refuse)
    monkeypatch.setattr("os.lchown", refuse)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    outside.chmod(0o600)
    worktree = tmp_path / "job"
    worktree.mkdir()
    (worktree / "link").symlink_to(outside)

    ContainerBackend(image="img").adopt_workdir(str(worktree))

    assert outside.stat().st_mode & 0o777 == 0o600, "a symlink target must be untouched"


def test_the_fallback_does_not_make_data_files_executable(tmp_path, monkeypatch):
    """`a+rwX`, not `a+rwx`: the execute bit only where it already meant something."""

    def refuse(*args, **kwargs):
        raise PermissionError("not root")

    monkeypatch.setattr("os.chown", refuse)
    monkeypatch.setattr("os.lchown", refuse)
    data = tmp_path / "data.json"
    data.write_text("{}")
    data.chmod(0o644)
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    ContainerBackend(image="img").adopt_workdir(str(tmp_path))

    assert not data.stat().st_mode & 0o111
    assert script.stat().st_mode & 0o111


# ── egress must fail closed ────────────────────────────────────────────


def test_an_unset_network_is_refused_not_defaulted_to_the_internet():
    """A missing setting must close the boundary, not open it.

    `network` used to fall back to `bridge`, so forgetting the variable gave
    untrusted repository code unrestricted outbound internet — the opposite of
    the rule the process backend already follows.
    """
    backend = build_backend_from_env(
        {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_IMAGE": "img:1"}
    )

    assert backend.network == "", "there must be no open-network fallback"
    # And nothing downstream invents one: resolving the agent phase's network
    # is an error rather than a quiet fall back to the Docker bridge.
    with pytest.raises((SandboxError, EgressPolicyError)):
        backend._check_networks_are_closed()
    with pytest.raises(EgressPolicyError):
        backend.network_for_phase("agent")


def test_open_egress_requires_an_explicit_acknowledgement():
    """Same shape as the unisolated-backend opt-in: deliberate, not accidental."""
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_SANDBOX_NETWORK": "some-open-network",
            "AGENT_SANDBOX_ALLOW_OPEN_NETWORK": "1",
        }
    )

    assert backend.allow_open_network is True
    backend._check_networks_are_closed()  # acknowledged: does not raise


def test_the_phase_decides_which_network_the_container_joins():
    """setup and agent must not silently share one network.

    Without this the tier model is decorative: the policy would say the agent
    turn is closed while both phases ran on whatever single network the backend
    was configured with.
    """
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY": "agent-egress",
            "AGENT_EGRESS_NETWORK_TRUSTED": "agent-setup",
        }
    )

    def network_of(phase: str) -> str:
        spec = SandboxSpec(argv=["x"], workdir="/w", phase=phase)
        command = backend.build_command(spec)
        return command[command.index("--network") + 1]

    assert network_of("agent") == "agent-egress"
    assert network_of("setup") == "agent-setup"


def test_the_bind_probe_uses_the_runtime_jobs_will_use():
    """Probing under runc while jobs run under kata validates nobody's config.

    The shipped default backend is kata, and the probe exists precisely to turn
    "every job dies at spawn with exit 125" into one startup failure — which it
    cannot do if it exercises a different runtime.
    """
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "kata",
            "AGENT_SANDBOX_IMAGE": "img:1",
            "AGENT_SANDBOX_NETWORK": "agent-egress",
        }
    )
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    import subprocess as sp

    original = sp.run
    sp.run = fake_run
    try:
        backend._check_bind_mountable("/var/lib/agent-jobs")
    finally:
        sp.run = original

    argv = captured["argv"]
    assert "--runtime" in argv
    assert argv[argv.index("--runtime") + 1] == KATA_RUNTIME
