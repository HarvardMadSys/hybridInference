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

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from serving.agent_jobs.runner import build_parser

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy/docker/docker-compose.agent-runner.yml"
_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/docker/Dockerfile.agent-sandbox"
_CODEX_REQUIREMENTS = Path(__file__).resolve().parents[2] / "deploy/docker/codex-requirements.toml"


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


def test_codex_mcp_is_constrained_to_an_empty_requirements_allowlist():
    """User, project, and plugin MCP servers are disabled after config merge.

    Codex 0.145 treats the presence of top-level ``mcp_servers`` requirements
    as the server allowlist. The same empty allowlist is applied a second time
    to plugin-provided MCP servers, so an empty table is stronger than
    ``--ignore-user-config`` alone.
    """
    requirements = tomllib.loads(_CODEX_REQUIREMENTS.read_text())
    assert requirements == {"mcp_servers": {}}
    assert "codex-requirements.toml /etc/codex/requirements.toml" in _DOCKERFILE.read_text()

    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/agent-job-runner.yml"
    assert "codex-requirements.toml /etc/codex/requirements.toml" in workflow.read_text()


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

    for name in ("CLAUDE_CODE_VERSION", "CODEX_VERSION", "PI_VERSION", "OPENCODE_VERSION"):
        pinned = re.search(rf"^ARG {name}=(\S+)$", image, re.MULTILINE)
        assert pinned, f"{name} must be pinned in the sandbox image"
        assert f'{name}: "{pinned.group(1)}"' in workflow, (
            f"{name} differs between the sandbox image and the Actions workflow"
        )

    # pi and OpenCode are reached through wrappers, so a substrate that
    # installs the CLI but not the wrapper produces jobs that die at spawn
    # with "binary missing" — both substrates must ship both.
    for wrapper in ("pi-freeinference", "opencode-freeinference"):
        assert wrapper in image, f"the sandbox image does not install {wrapper}"
        assert wrapper in workflow, f"the Actions runner does not install {wrapper}"


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


_STAGING_DEPLOY = Path(__file__).resolve().parents[2] / "ops/deploy/deploy_staging.sh"
_MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"


def test_staging_deploy_owns_the_runner_when_the_host_opts_in():
    """The runner is part of the deploy, not a hand-started orphan.

    P0 shipped runner code with no standing deployment: every queued job
    waited for someone to start a runner by hand, and the next `make build`
    on the host would not have known the overlay existed. The staging deploy
    now enables the overlay when the host sets AGENT_DISPATCHER_TOKEN — and
    it must ride the SAME compose invocation as the main stack, because the
    overlay attaches `backend` to the agent-egress network.
    """
    script = _STAGING_DEPLOY.read_text()
    assert "AGENT_DISPATCHER_TOKEN=" in script, (
        "opt-in must key off the credential the runner needs anyway"
    )
    assert "docker-compose.agent-runner.yml" in script, (
        "the overlay never enters the deploy's compose file set"
    )
    assert "Dockerfile.agent-sandbox" in script, (
        "nothing builds the sandbox image the overlay requires"
    )
    assert 'AGENT_RUNNER="$AGENT_RUNNER"' in script, (
        "the flag never reaches make, so the overlay is dropped at the up step"
    )

    makefile = _MAKEFILE.read_text()
    assert "docker-compose.agent-runner.yml" in makefile, (
        "make build must include the overlay when AGENT_RUNNER=1 — a separate "
        "compose call would recreate backend without the distribution env files"
    )


def test_compose_file_has_no_duplicate_keys():
    """Duplicate mapping keys make docker compose reject the whole file.

    `yaml.safe_load` keeps the last of a repeated key, so every test in this
    module passed while `docker compose` refused to parse the overlay at all
    ("mapping key already defined") — the runner could not have started from
    it on any host. Python's tolerance is what hid it; this loader is as
    strict as the Go parser that actually reads the file.
    """

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _no_duplicates(loader, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(
                    f"duplicate key {key!r} in {_COMPOSE.name}: docker compose "
                    "rejects the entire file, so the runner never starts"
                )
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)
    yaml.load(_COMPOSE.read_text(), Loader=_StrictLoader)


_PI_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/docker/pi-freeinference"


def test_pi_wrapper_writes_the_provider_config_and_execs_pi(tmp_path):
    """The wrapper turns its environment into pi's models.json, argv untouched.

    Executed for real rather than read: a stub ``pi`` on PATH records the argv
    it receives, HOME is a temp dir, and the assertions read the exact file the
    real pi would read. pi ignores OPENAI_BASE_URL, so this file is the ONLY
    thing standing between a job and api.openai.com.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "argv.json"
    stub = stub_dir / "pi"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    prompt = 'prompt; with $(dangerous) `chars` "quoted"'
    result = _subprocess.run(
        [_sys.executable, str(_PI_WRAPPER), "--provider", "freeinference", "-p", prompt],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "PI_GATEWAY_MODEL": "glm-5.1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    provider = _json.loads((home / ".pi/agent/models.json").read_text())["providers"][
        "freeinference"
    ]
    assert provider["baseUrl"] == "http://backend:8080/v1"
    assert provider["apiKey"] == "ajt.attempt.key"
    assert provider["api"] == "openai-completions"
    assert provider["models"] == [{"id": "glm-5.1"}]
    # argv passed through byte-for-byte: the shell-hostile prompt survived.
    assert _json.loads(record.read_text()) == [
        "--provider",
        "freeinference",
        "-p",
        prompt,
    ]


def test_pi_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a job that dials OpenAI."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_PI_WRAPPER), "-p", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and PI_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


_OPENCODE_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/docker/opencode-freeinference"


def test_opencode_wrapper_writes_config_sets_offline_flags_and_execs(tmp_path):
    """The wrapper produces OpenCode's config and kills its phone-home paths.

    A stub ``opencode`` on PATH records argv and the environment it received.
    The offline flags are the load-bearing part: without
    OPENCODE_DISABLE_MODELS_FETCH the CLI hard-fails fetching models.dev,
    which in the deny-all sandbox is every single run.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "seen.json"
    stub = stub_dir / "opencode"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "seen = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()"
        " if k.startswith('OPENCODE_')}}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(seen))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    prompt = 'fix; the $(bug) "carefully"'
    result = _subprocess.run(
        [
            _sys.executable,
            str(_OPENCODE_WRAPPER),
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            "freeinference/glm-5.1",
            prompt,
        ],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "OPENCODE_GATEWAY_MODEL": "glm-5.1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    seen = _json.loads(record.read_text())
    assert seen["argv"] == [
        "run",
        "--format",
        "json",
        "--auto",
        "-m",
        "freeinference/glm-5.1",
        prompt,
    ]
    assert seen["env"]["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # OpenCode v1.18.9's supported switch prevents both project opencode.json
    # and project .opencode/ directories from entering the merged config.
    assert seen["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"

    config = _json.loads(Path(seen["env"]["OPENCODE_CONFIG"]).read_text())
    provider = config["provider"]["freeinference"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://backend:8080/v1"
    assert provider["options"]["apiKey"] == "ajt.attempt.key"
    assert provider["models"] == {"glm-5.1": {"name": "glm-5.1"}}
    # The config lives in HOME, never in the job worktree.
    assert seen["env"]["OPENCODE_CONFIG"].startswith(str(home))


def test_opencode_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a run that dials out."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_OPENCODE_WRAPPER), "run", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and OPENCODE_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


def test_concurrency_is_declared_not_typed_at_deploy_time(compose: dict):
    """One runner takes one job at a time, so replicas *is* the concurrency.

    A `--scale` passed by hand survives until the next `docker compose up`
    without it, which silently drops the fleet back to one — presenting as
    "every user is queueing" long after anyone remembers scaling it. The count
    therefore belongs in the file the deploy reads.
    """
    runner = compose["services"]["agent-runner"]
    replicas = (runner.get("deploy") or {}).get("replicas")
    assert replicas, "agent-runner declares no replica count, so a deploy resets concurrency to 1"
    assert "AGENT_RUNNER_REPLICAS" in str(replicas), "the replica count is not operator-tunable"


def test_replicas_are_distinguishable_in_the_lease_ledger(compose: dict):
    """Replicas must not all claim jobs under the same lease_owner.

    Fencing never reads this string, so a collision is not a correctness bug —
    it just makes "which runner is stuck" unanswerable exactly when a second
    replica makes it worth asking.
    """
    command = " ".join(compose["services"]["agent-runner"].get("command") or [])
    assert "--worker-id" not in command, (
        "a literal --worker-id pins every replica to the same lease_owner; "
        "let the runner derive one that includes its hostname"
    )


def test_runner_derives_a_distinct_worker_id_per_container(monkeypatch):
    """The derived id groups by configuration and separates by host."""
    from serving.agent_jobs.runner import default_worker_id

    monkeypatch.setattr("socket.gethostname", lambda: "abc123")
    monkeypatch.delenv("AGENT_WORKER_ID", raising=False)
    assert default_worker_id() == "runner-abc123"

    monkeypatch.setenv("AGENT_WORKER_ID", "staging")
    assert default_worker_id() == "staging-abc123"
