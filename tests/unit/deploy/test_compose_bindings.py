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


def _render(tmp_path: Path, docker_cli: str, host: str | None, *, example: bool = False) -> dict:
    """Use only fixture environment values, never a developer's deployment."""
    env_file = tmp_path / "compose.env"
    settings = []
    args = [docker_cli, "compose", "-f", str(COMPOSE)]
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
        "frontend": [(expected_host, "3001", 3001)],
        # The API, and the agent entry beside it: loopback both, whatever the
        # console's host (serving/servers/agent_entry.py).
        "backend": [("127.0.0.1", "8080", 8080), ("127.0.0.1", "8090", 8090)],
        "postgres": [("127.0.0.1", "5432", 5432)],
    }.items():
        ports = services[name]["ports"]
        assert [(p["host_ip"], p["published"], p["target"]) for p in ports] == expected
    assert services["frontend"]["environment"]["PORT"] == "3001"
    assert services["backend"]["environment"]["GATEWAY_AGENT_ENTRY_PORT"] == "8090"


@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_example_keeps_its_local_ports_and_accepts_a_host_override(
    tmp_path: Path, docker_cli: str, host: str | None
) -> None:
    """The Stage 2 example's env file and overlays retain their port choices."""
    services = _render(tmp_path, docker_cli, host, example=True)["services"]
    for name, expected in {
        "frontend": [(host or "127.0.0.1", "13001", 3001)],
        "backend": [("127.0.0.1", "18080", 8080), ("127.0.0.1", "8090", 8090)],
        "postgres": [("127.0.0.1", "15432", 5432)],
    }.items():
        ports = services[name]["ports"]
        assert [(p["host_ip"], p["published"], p["target"]) for p in ports] == expected
