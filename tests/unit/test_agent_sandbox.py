"""Unit tests for pluggable sandbox backends.

The isolation flags are the security surface here, so they are asserted on the
composed command rather than trusted to a container runtime being installed.
"""

from __future__ import annotations

import pytest

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
    backend = build_backend_from_env({"AGENT_SANDBOX_BACKEND": "kata"})
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
        {"AGENT_SANDBOX_BACKEND": "container", "AGENT_SANDBOX_RUNTIME": "runsc"}
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
