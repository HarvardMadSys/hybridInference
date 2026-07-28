"""Static checks on the self-hosted runner deployment.

A compose file cannot be unit-tested by running it, but the properties that
matter here are structural: an egress network that is not actually internal, or
a flag the runner does not implement, are silent failures — the first weakens
the sandbox boundary without anyone noticing, the second only shows up when a
runner container crash-loops in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from serving.agent_jobs.runner import build_parser

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker/docker-compose.agent-runner.yml"
_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/docker/Dockerfile.agent-sandbox"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parse the runner compose overlay."""
    return yaml.safe_load(_COMPOSE.read_text())


def test_egress_network_is_internal(compose: dict):
    """The sandbox network must have no route off the host.

    This is the PlatformOnly tier expressed as infrastructure: a sandbox on an
    internal network reaches the gateway and nothing else, whatever the agent
    inside decides to try.
    """
    assert compose["networks"]["agent-egress"]["internal"] is True


def test_runner_mounts_the_docker_socket_read_only(compose: dict):
    """The runner starts sandboxes; it has no business reconfiguring the daemon."""
    volumes = compose["services"]["agent-runner"]["volumes"]
    socket_mounts = [v for v in volumes if "docker.sock" in v]
    assert socket_mounts, "the runner needs the daemon to spawn sandboxes"
    assert all(v.endswith(":ro") for v in socket_mounts)


def test_default_backend_is_vm_isolated(compose: dict):
    """Self-hosted defaults to a kernel boundary per job, not a shared one."""
    env = compose["services"]["agent-runner"]["environment"]
    assert "kata" in env["AGENT_SANDBOX_BACKEND"]


def test_dispatcher_credential_is_required_not_defaulted(compose: dict):
    """Starting a runner without a dispatcher credential must fail loudly."""
    env = compose["services"]["agent-runner"]["environment"]
    assert ":?" in env["AGENT_DISPATCHER_TOKEN"]


def test_compose_only_uses_flags_the_runner_implements(compose: dict):
    """Every flag in the compose command must exist, or the container crash-loops."""
    command = compose["services"]["agent-runner"]["command"]
    flags = {arg for arg in command if isinstance(arg, str) and arg.startswith("--")}
    known = {
        action_option
        for action in build_parser()._actions
        for action_option in action.option_strings
    }
    assert flags <= known, f"compose passes unknown flags: {flags - known}"


def test_sandbox_image_carries_no_credentials():
    """No secret may be baked into the image the agent runs in."""
    text = _DOCKERFILE.read_text()
    for forbidden in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AGENT_DISPATCHER_TOKEN", "hyi-"):
        assert forbidden not in text, f"{forbidden} must never be baked into the sandbox image"


def test_sandbox_image_runs_as_non_root():
    """A kernel escape should have to start from an unprivileged user."""
    text = _DOCKERFILE.read_text()
    assert "USER agent" in text
    assert text.index("USER agent") < text.index("WORKDIR /workspace")


def test_sandbox_image_disables_agent_phone_home():
    """Telemetry and auto-update are off, so a blocked request is not a mystery timeout."""
    text = _DOCKERFILE.read_text()
    for flag in (
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        assert flag in text
