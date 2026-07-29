"""Unit tests for pluggable sandbox backends.

The isolation flags are the security surface here, so they are asserted on the
composed command rather than trusted to a container runtime being installed.
"""

from __future__ import annotations

import pytest

from serving.agent_jobs.egress import EgressPolicyError
from serving.agent_jobs.sandbox import (
    KATA_RUNTIME,
    ContainerBackend,
    ProcessBackend,
    SandboxError,
    SandboxSpec,
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


def test_plain_container_is_not_vm_isolated():
    """A shared-kernel container must not claim VM isolation."""
    backend = ContainerBackend()
    assert backend.is_vm_isolated is False
    assert "--runtime" not in backend.build_command(_SPEC)


def test_kata_backend_requests_the_vm_runtime():
    """Selecting kata puts a kernel boundary around each job."""
    backend = build_backend_from_env(
        {"AGENT_SANDBOX_BACKEND": "kata", "AGENT_SANDBOX_NETWORK": "agent-egress"}
    )
    assert isinstance(backend, ContainerBackend)
    assert backend.is_vm_isolated is True
    command = backend.build_command(_SPEC)
    assert command[command.index("--runtime") + 1] == KATA_RUNTIME


def test_env_reaches_the_container_as_env_flags_not_inherited():
    """Agent environment is passed explicitly, never inherited by accident."""
    command = ContainerBackend().build_command(
        SandboxSpec(argv=["x"], workdir="/w", env={"TOKEN": "secret", "B": "2"})
    )
    assert "TOKEN=secret" in command
    assert command.count("--env") == 2


def test_backend_selection_from_env():
    """The three configured shapes resolve to the right backend."""
    assert isinstance(build_backend_from_env({}), ProcessBackend)
    assert isinstance(
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "container"}), ContainerBackend
    )
    with pytest.raises(SandboxError):
        build_backend_from_env({"AGENT_SANDBOX_BACKEND": "nonsense"})


def test_explicit_runtime_override_wins():
    """An operator can name a different VM runtime than the Kata default."""
    backend = build_backend_from_env(
        {
            "AGENT_SANDBOX_BACKEND": "container",
            "AGENT_SANDBOX_RUNTIME": "runsc",
            "AGENT_SANDBOX_NETWORK": "agent-egress",
        }
    )
    assert backend.is_vm_isolated is True
    assert backend.build_command(_SPEC)[backend.build_command(_SPEC).index("--runtime") + 1] == (
        "runsc"
    )


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
    backend = build_backend_from_env({"AGENT_SANDBOX_BACKEND": "container"})

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
        {"AGENT_SANDBOX_BACKEND": "kata", "AGENT_SANDBOX_NETWORK": "agent-egress"}
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
