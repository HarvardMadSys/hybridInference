"""Static checks on the self-hosted runner deployment.

A compose file cannot be unit-tested by running it, but the properties that
matter here are structural: an egress network that is not actually internal, or
a flag the runner does not implement, are silent failures — the first weakens
the sandbox boundary without anyone noticing, the second only shows up when a
runner container crash-loops in production.
"""

from __future__ import annotations

import re
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


# ── the chain from compose file to a running sandbox ───────────────────
#
# Each of these encodes a way the self-hosted deployment failed while every
# individual file looked correct in isolation. They are static because the
# alternative is discovering them one at a time on a remote host.

_RUNNER_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/docker/Dockerfile.agent-runner"


def _split_mount(mount: str) -> list[str]:
    """Split a compose volume on its separators, not on the ones inside ``${}``.

    ``${VAR:-/default}`` contains a colon of its own, so a naive split reports
    a mismatch that is not there.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for char in mount:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == ":" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def _expand(value: str) -> str:
    """Resolve ``${VAR:-default}`` to its default, as an unset environment would."""
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", value)


def test_the_egress_network_name_is_pinned(compose: dict):
    """Compose prefixes generated network names with the project name.

    The runner passes this value straight to ``docker run --network``, so
    without an explicit name the sandbox is asked to join `agent-egress` while
    the network that exists is `hybridinference_agent-egress`, and every spawn
    fails.
    """
    assert compose["networks"]["agent-egress"]["name"] == "agent-egress"


def test_the_gateway_is_reachable_from_the_sandbox_network(compose: dict):
    """An internal network with only the sandbox on it is a network to nowhere.

    Model calls and event reporting both go to the gateway; if it is not on
    this network the sandbox fails at its first request.
    """
    assert "agent-egress" in compose["services"]["backend"]["networks"]


def test_job_worktrees_are_a_host_path_at_the_same_path_inside(compose: dict):
    """The daemon resolves the runner's bind source on the *host*.

    A named volume satisfies the runner's own file operations and then fails
    every `docker run --mount` with an opaque exit 125, because the path exists
    only inside the runner container.
    """
    volumes = compose["services"]["agent-runner"]["volumes"]
    workdir_mounts = [v for v in volumes if "agent-jobs" in v]
    assert workdir_mounts, "the runner needs somewhere to put job worktrees"
    for mount in workdir_mounts:
        source, target = _split_mount(mount)[:2]
        assert source == target, f"bind source and target must match, got {mount}"
        resolved = _expand(source)
        assert resolved.startswith("/"), "must be a host path, not a named volume"


def test_the_runner_image_can_actually_start_a_sandbox():
    """The container backend shells out to `docker`; the backend image has none."""
    text = _RUNNER_DOCKERFILE.read_text()
    assert "docker:" in text and "/usr/local/bin/docker" in text


def test_the_runner_image_can_check_a_repository_out():
    """No git in the runner means no worktree, and an agent with nothing to read."""
    assert "git" in _RUNNER_DOCKERFILE.read_text()


def test_the_runner_service_uses_the_runner_image(compose: dict):
    """Built from the backend image, the runner has neither docker nor git."""
    dockerfile = compose["services"]["agent-runner"]["build"]["dockerfile"]
    assert dockerfile.endswith("Dockerfile.agent-runner")


def test_the_sandbox_uid_matches_what_the_runner_chowns_to():
    """The runner gives each worktree to this exact id before mounting it.

    Drift here does not fail loudly: the agent simply cannot write to its own
    working tree, and git refuses to run at all with "dubious ownership".
    """
    from serving.agent_jobs.sandbox import SANDBOX_GID, SANDBOX_UID

    text = _DOCKERFILE.read_text()
    assert f"--uid {SANDBOX_UID}" in text
    assert f"--gid {SANDBOX_GID}" in text
