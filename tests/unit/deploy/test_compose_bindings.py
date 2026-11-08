"""Render Compose to verify host exposure, including env files and overlays.

The Compose CLI needs no Docker daemon for these checks. Skip when the CLI is
unavailable so ordinary unit tests still run on machines without Docker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ops.admin import gateway_ops

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
EXAMPLE = REPO / "distributions" / "example" / "deploy"


@pytest.fixture(scope="module")
def docker_cli() -> str:
    """Find Compose without requiring a running Docker daemon."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("the Docker Compose CLI is not installed")
    version = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=30, check=False
    )
    if version.returncode:
        pytest.skip("the Docker Compose plugin is not installed")
    return docker


def _render(
    tmp_path: Path,
    docker_cli: str,
    host: str | None,
    *,
    example: bool = False,
    overlays: tuple[Path, ...] = (),
    extra: tuple[str, ...] = (),
    env_files: tuple[Path, ...] = (),
) -> dict:
    """Use only fixture environment values, never a developer's deployment.

    *env_files* come before the fixture's own, as a distribution's deploy/*.env
    files come before .env; unlike that one, they are no container's env_file.
    """
    env_file = tmp_path / "compose.env"
    settings = list(extra)
    args = [docker_cli, "compose", "-f", str(COMPOSE)]
    for overlay in overlays:
        args.extend(["-f", str(overlay)])
    if example:
        for overlay in ("docker-compose.yml", "docker-compose.demo.yml"):
            args.extend(["-f", str(EXAMPLE / overlay)])
        args.extend(["--env-file", str(EXAMPLE / "backend.env")])
    else:
        settings.extend(
            [
                f"BACKEND_ENV_FILE={env_file}",
                "DB_NAME=compose_test",
                "DB_USER=compose_test",
                "DB_PASSWORD=local-compose-test-only",
            ]
        )
    if host is not None:
        settings.append(f"FRONTEND_HOST={host}")
    env_file.write_text("\n".join(settings) + "\n")
    for path in env_files:
        args.extend(["--env-file", str(path)])
    args.extend(["--env-file", str(env_file), "config", "--format", "json"])
    # Keep Docker's CLI discovery environment, but exclude ambient Compose and
    # application settings which would override the env files under test.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in os.environ}
    result = subprocess.run(
        args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("host", "expected_host"),
    [(None, "127.0.0.1"), ("", "127.0.0.1"), ("0.0.0.0", "0.0.0.0"), ("192.0.2.10", "192.0.2.10")],
)
def test_host_ports_default_to_loopback_and_allow_explicit_frontend_binding(
    tmp_path: Path, docker_cli: str, host: str | None, expected_host: str
) -> None:
    """Unset/blank hosts stay local; explicit hosts preserve operator intent."""
    services = _render(tmp_path, docker_cli, host)["services"]
    for name, expected in {
        "frontend": (expected_host, "3001", 3001),
        "backend": ("127.0.0.1", "8080", 8080),
        "postgres": ("127.0.0.1", "5432", 5432),
    }.items():
        (port,) = services[name]["ports"]
        assert (port["host_ip"], port["published"], port["target"]) == expected
    assert services["frontend"]["environment"]["PORT"] == "3001"


@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_example_keeps_its_local_ports_and_accepts_a_host_override(
    tmp_path: Path, docker_cli: str, host: str | None
) -> None:
    """The Stage 2 example's env file and overlays retain their port choices."""
    services = _render(tmp_path, docker_cli, host, example=True)["services"]
    for name, expected in {
        "frontend": (host or "127.0.0.1", "13001", 3001),
        "backend": ("127.0.0.1", "18080", 8080),
        "postgres": ("127.0.0.1", "15432", 5432),
    }.items():
        (port,) = services[name]["ports"]
        assert (port["host_ip"], port["published"], port["target"]) == expected


@pytest.mark.parametrize("published", [False, True])
def test_pgadmin_renders_with_its_own_login_and_no_port_unless_published(
    tmp_path: Path, docker_cli: str, published: bool
) -> None:
    """No password set must not stop Compose: pgAdmin refuses a new volume itself.

    And with the profile on, pgAdmin keeps its login and publishes nothing
    unless the opt-in overlay is named, which binds loopback only.
    """
    overlays = (COMPOSE.parent / "docker-compose.pgadmin-port.yml",) if published else ()
    pgadmin = _render(
        tmp_path, docker_cli, None, overlays=overlays, extra=("COMPOSE_PROFILES=admin",)
    )["services"]["pgadmin"]

    assert pgadmin["environment"]["PGADMIN_CONFIG_SERVER_MODE"] == "True"
    assert pgadmin["environment"]["PGADMIN_DEFAULT_PASSWORD"] == ""
    if published:
        (port,) = pgadmin["ports"]
        assert (port["host_ip"], port["published"], port["target"]) == ("127.0.0.1", "5050", 80)
    else:
        assert "ports" not in pgadmin


@pytest.mark.parametrize(
    ("distribution_profiles", "local_profiles", "persisted_profiles", "admin_enabled"),
    [
        ("admin", None, None, True),
        (None, "admin", None, True),
        ("admin", "", None, False),
        (None, "", "admin", True),
        (None, "admin", "", False),
    ],
)
def test_make_preserves_compose_profile_env_file_precedence(
    tmp_path: Path,
    docker_cli: str,
    distribution_profiles: str | None,
    local_profiles: str | None,
    persisted_profiles: str | None,
    admin_enabled: bool,
) -> None:
    """An undefined Make variable must leave profile selection to Compose's env files."""
    shutil.copy(REPO / "Makefile", tmp_path / "Makefile")
    compose = tmp_path / "deploy" / "docker" / "docker-compose.yml"
    compose.parent.mkdir(parents=True)
    shutil.copy(COMPOSE, compose)
    overlay = tmp_path / "distributions" / "profiles" / "deploy" / "backend.env"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        f"COMPOSE_PROFILES={distribution_profiles}\n" if distribution_profiles is not None else ""
    )
    local = "DB_NAME=compose_test\nDB_USER=compose_test\nDB_PASSWORD=local-compose-test-only\n"
    if local_profiles is not None:
        local += f"COMPOSE_PROFILES={local_profiles}\n"
    (tmp_path / ".env").write_text(local)
    if persisted_profiles is not None:
        (tmp_path / ".ops.env").write_text(f"COMPOSE_PROFILES={persisted_profiles}\n")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in os.environ}
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "--eval=profile-services: ; @$(COMPOSE) config --services",
            "profile-services",
            "DISTRIBUTION=profiles",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ("pgadmin" in result.stdout.splitlines()) is admin_enabled


@pytest.mark.parametrize("local_override", [False, True])
def test_probe_urls_follow_make_extra_env_files_and_local_overrides(
    tmp_path: Path, docker_cli: str, monkeypatch, local_override: bool
) -> None:
    """Probe the effective Compose ports after deployment and host overrides merge."""
    shutil.copy(REPO / "Makefile", tmp_path / "Makefile")
    compose = tmp_path / "deploy" / "docker" / "docker-compose.yml"
    compose.parent.mkdir(parents=True)
    shutil.copy(COMPOSE, compose)
    (tmp_path / "staging.env").write_text("BACKEND_PORT=28080\nFRONTEND_PORT=23001\n")
    local = "DB_NAME=compose_test\nDB_USER=compose_test\nDB_PASSWORD=local-compose-test-only\n"
    if local_override:
        local += "BACKEND_PORT=38080\nFRONTEND_PORT=33001\n"
    (tmp_path / ".env").write_text(local)
    env = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG") if key in os.environ}
    original_command = gateway_ops.command

    def render_with_make(argv, **kwargs):
        return original_command(
            [
                "make",
                "--no-print-directory",
                "--eval=probe-config: ; @$(COMPOSE) config --format json",
                "probe-config",
                "DISTRIBUTION=none",
                "COMPOSE_EXTRA_ENV_FILES=staging.env",
            ],
            cwd=tmp_path,
            env=env,
            **kwargs,
        )

    monkeypatch.setattr(gateway_ops, "command", render_with_make)
    assert gateway_ops.published_base_url([], "frontend") == (
        "http://127.0.0.1:33001" if local_override else "http://127.0.0.1:23001"
    )
    assert gateway_ops.published_base_url([], "backend") == (
        "http://127.0.0.1:38080" if local_override else "http://127.0.0.1:28080"
    )


# What a deployment of the release before database-backed configuration names
# in its overlay's deploy/*.env: the previous image reads its files through them.
_RETIRED_SELECTORS = {
    "DISTRIBUTION_CONFIG_PATH": "/app/distributions/site/distribution.yaml",
    "DISTRIBUTION_CONFIG_MODE": "active",
    "MODELS_CONFIG_PATH": "/app/distributions/site/config/models.yaml",
    "ROUTING_CONFIG_PATH": "/app/distributions/site/config/routing.yaml",
    "ALERTS_CONFIG_PATH": "/app/distributions/site/config/alerts.yaml",
}
_PINNED = (
    "BACKEND_IMAGE=registry.example/backend@sha256:" + "a" * 64,
    "FRONTEND_IMAGE=registry.example/frontend@sha256:" + "b" * 64,
)


@pytest.mark.parametrize(
    "overlay",
    [None, "docker-compose.artifacts.yml", "docker-compose.digest-backend.yml"],
    ids=["source", "deploy-and-rollback", "digest"],
)
def test_a_rolled_back_backend_still_gets_its_configuration_selectors(
    tmp_path: Path, docker_cli: str, overlay: str | None
) -> None:
    """A failed deploy or `make rollback` starts the previous image from these files.

    That image reads its manifest, models, routing and alert files through
    these variables; without them it reopens public signup, drops the
    manifest's feature restrictions and branding, and alerts on the built-in
    thresholds. Every way a pinned image is started must carry them.
    """
    overlays = (COMPOSE.parent / overlay,) if overlay else ()
    # The overlay's env file, not .env: Compose reads it, the container does not.
    deployment = tmp_path / "backend.env"
    deployment.write_text("".join(f"{key}={value}\n" for key, value in _RETIRED_SELECTORS.items()))
    backend = _render(
        tmp_path, docker_cli, None, overlays=overlays, extra=_PINNED, env_files=(deployment,)
    )["services"]["backend"]

    for key, value in _RETIRED_SELECTORS.items():
        assert backend["environment"].get(key) == value, key


def test_unset_configuration_selectors_stay_out_of_the_container(
    tmp_path: Path, docker_cli: str
) -> None:
    """Unset must stay unset: the previous release then defaults to dark mode.

    An empty mode is an error there, and a path default would outrank the
    manifest, so Compose may only pass on what a deployment sets.
    """
    environment = _render(tmp_path, docker_cli, None)["services"]["backend"]["environment"]

    for key in _RETIRED_SELECTORS:
        assert environment.get(key) is None, key


def test_the_restore_drill_server_shares_nothing_with_the_deployment_database(
    tmp_path: Path, docker_cli: str
) -> None:
    """`make restore-check` restores into this server, so it must stand apart.

    Its own volume and an internal network that only the migrating one-off
    backend joins; no published port; the deployment's image and credentials.
    """
    config = _render(
        tmp_path,
        docker_cli,
        None,
        overlays=(COMPOSE.parent / "docker-compose.restore-check.yml",),
        extra=("RESTORE_CHECK_NAME=gateway-restore-test", "RESTORE_CHECK_IMAGE=postgres:16.15"),
    )
    scratch = config["services"]["restore-check"]
    deployment = config["services"]["postgres"]

    assert scratch["image"] == deployment["image"]
    assert scratch["container_name"] == "gateway-restore-test"
    assert "ports" not in scratch
    (volume,) = scratch["volumes"]
    assert volume["target"] == "/var/lib/postgresql/data"
    assert config["volumes"][volume["source"]]["name"] == "gateway-restore-test"
    assert volume["source"] not in {mount["source"] for mount in deployment["volumes"]}
    assert set(scratch["networks"]) == {"restore-check"}
    assert config["networks"]["restore-check"] == {
        "name": "gateway-restore-test",
        "internal": True,
        "ipam": {},
    }
    assert "restore-check" in config["services"]["backend"]["networks"]
    assert "restore-check" not in deployment["networks"]
    for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        assert scratch["environment"][key] == deployment["environment"][key]
