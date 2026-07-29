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
    # Interpolated off the tier variable, so the network compose creates, the
    # one the gateway joins and the one the runner spawns onto cannot diverge.
    assert _expand(compose["networks"]["agent-egress"]["name"]) == "agent-egress"


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


def test_agent_runtimes_are_pinned_not_floating():
    """`latest` in the image is a dependency that changes under you between builds.

    It already cost a job: `latest` resolved to Claude Code 2.1.220, whose
    request shape the gateway rejected, and every run died at its first model
    call with an error that pointed at the model rather than the CLI version.
    """
    text = _DOCKERFILE.read_text()
    versions = dict(re.findall(r"^ARG (\w+_VERSION)=(\S+)$", text, re.MULTILINE))
    assert versions, "the image must declare runtime versions"
    for name, value in versions.items():
        assert value != "latest", f"{name} must be pinned, not 'latest'"
        assert re.fullmatch(r"\d+\.\d+\.\d+", value), f"{name}={value} is not an exact version"


def test_both_phases_are_closed_in_the_shipped_configuration(compose: dict):
    """The overlay ships no setup network, so neither phase may claim `trusted`.

    The design's external-beta default is setup=trusted, but there is no setup
    phase yet and no allowlist-fronted network to run it on. Shipping `trusted`
    as the compose default would name a tier this deployment cannot honour —
    the config would read as "dependencies can be installed" while the network
    behind it reaches only the gateway.
    """
    env = compose["services"]["agent-runner"]["environment"]
    # And the spawn network tracks the same variable the network block names.
    assert _expand(env["AGENT_EGRESS_NETWORK_PLATFORM_ONLY"]) == _expand(
        compose["networks"]["agent-egress"]["name"]
    )
    for var in ("AGENT_EGRESS_SETUP_TIER", "AGENT_EGRESS_AGENT_TIER"):
        assert "platform_only" in env[var], f"{var} must be closed until a tier exists for it"


def test_both_substrates_pin_the_same_agent_cli_versions():
    """A job must not behave differently depending on which substrate claims it.

    The Actions runner installs the CLIs itself while the container backend
    gets them from the sandbox image. When those drift, the same job produces
    different results — or fails on one substrate only — for a reason nothing
    in the job's own record explains. `latest` on the Actions side already cost
    a real run once.
    """
    # One of the two substrates is the Actions workflow, and the public export
    # replaces .github/workflows/ wholesale — so in an exported tree there is
    # no second thing to agree with, and "they pin the same versions" is not a
    # claim that can be false there. Skipped rather than excluding the file:
    # the other twenty cases here assert the sandbox image and the runner
    # compose, both of which travel and are worth running downstream.
    path = Path(__file__).resolve().parents[2] / ".github/workflows/agent-job-runner.yml"
    if not path.exists():
        pytest.skip("no Actions workflow in this tree — only one substrate to check")
    workflow = path.read_text()
    image = _DOCKERFILE.read_text()

    for name in ("CLAUDE_CODE_VERSION", "CODEX_VERSION"):
        pinned = re.search(rf"^ARG {name}=(\S+)$", image, re.MULTILINE)
        assert pinned, f"{name} must be pinned in the sandbox image"
        assert f'{name}: "{pinned.group(1)}"' in workflow, (
            f"{name} differs between the sandbox image and the Actions workflow"
        )


def test_the_runner_reads_the_bounds_the_overlay_configures(monkeypatch, compose: dict):
    """A configured lease and timeout must actually reach the runner.

    The overlay set both and the runner read neither, so every self-hosted job
    ran on the built-in defaults no matter what the operator wrote — a setting
    that looks applied and is not.
    """
    env = compose["services"]["agent-runner"]["environment"]
    assert "AGENT_LEASE_TTL" in env and "AGENT_TIMEOUT_S" in env

    monkeypatch.setenv("AGENT_LEASE_TTL", "45")
    monkeypatch.setenv("AGENT_TIMEOUT_S", "600")
    args = build_parser().parse_args([])

    assert args.lease_ttl == 45.0
    assert args.agent_timeout == 600.0


def test_the_runner_reads_the_neutral_gateway_variable(monkeypatch):
    """The self-hosted runner uses the same gateway variable as compose."""
    monkeypatch.setenv("AGENT_GATEWAY_URL", "https://gateway.example.com")

    args = build_parser().parse_args([])

    assert args.base_url == "https://gateway.example.com"


def test_the_runner_keeps_the_deployment_gateway_alias(monkeypatch):
    """Existing hosts keep working while they migrate to the neutral name."""
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    monkeypatch.setenv("FREEINFERENCE_BASE_URL", "https://legacy-gateway.example.com")

    args = build_parser().parse_args([])

    assert args.base_url == "https://legacy-gateway.example.com"


def test_an_unparsable_bound_falls_back_rather_than_crash_looping(monkeypatch):
    """A typo in an env var must not take the runner down on every restart."""
    monkeypatch.setenv("AGENT_LEASE_TTL", "two minutes")
    args = build_parser().parse_args([])
    assert args.lease_ttl > 0


def test_every_tier_the_overlay_lets_you_select_is_passed_through(compose: dict):
    """Selecting a tier must also deliver that tier's network to the container.

    The overlay offered the tier variables while passing through only the
    `platform_only` network, so an operator who set
    AGENT_EGRESS_AGENT_TIER=custom *and* AGENT_EGRESS_NETWORK_CUSTOM in their
    .env got the first and not the second — and the runner refused at preflight
    with "names no network" for a variable they had plainly set. Found by
    running the overlay, not by reading it.
    """
    env = compose["services"]["agent-runner"]["environment"]
    selectable = {
        "platform_only": "PLATFORM_ONLY",
        "trusted": "TRUSTED",
        "custom": "CUSTOM",
        "full": "FULL",
    }
    for tier, suffix in selectable.items():
        assert f"AGENT_EGRESS_NETWORK_{suffix}" in env, (
            f"tier {tier!r} is selectable but its network variable never reaches the runner"
        )
