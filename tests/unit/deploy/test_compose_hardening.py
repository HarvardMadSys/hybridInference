"""What the base Compose file grants each container, and what it holds back.

Read from the YAML rather than rendered, so the checks need neither Docker nor
an env file; ``test_compose_bindings.py`` renders the file when the Compose CLI
is installed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
OVERLAYS = sorted((REPO / "deploy" / "docker").glob("docker-compose.*.yml"))
FRONTEND_DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile.frontend"


def _services(path: Path = COMPOSE) -> dict:
    return yaml.safe_load(path.read_text())["services"]


ROTATED = {"driver": "json-file", "options": {"max-size": "100m", "max-file": "5"}}


def test_every_service_rotates_its_log() -> None:
    """json-file keeps a container's whole log otherwise, on the disk Postgres uses.

    The backend logs request headers at INFO, so a client sending large
    headers could fill that disk.
    """
    for name, service in _services().items():
        assert service.get("logging") == ROTATED, name


def test_the_overlays_keep_the_base_log_rotation() -> None:
    """A service an overlay adds, or whose logging it sets, would escape the rotation."""
    assert OVERLAYS, "the overlay scan went blind"
    base = _services()
    for overlay in OVERLAYS:
        for name, service in _services(overlay).items():
            assert name in base, f"{overlay.name} adds {name}, which the base does not rotate"
            assert "logging" not in service, f"{overlay.name} overrides {name}'s logging"


def test_the_env_example_writes_no_log_file() -> None:
    """LOG_FILE is a plain file handler: inside a container it grows unrotated."""
    lines = (REPO / ".env.example").read_text().splitlines()

    assert not [line for line in lines if line.strip().startswith("LOG_FILE=")]


def test_the_console_has_a_memory_ceiling() -> None:
    """Request bodies pass through the console's memory on their way to the API.

    Without a ceiling, enough of them at once starve everything else on the
    host; with one, the console alone restarts.
    """
    frontend = _services()["frontend"]

    assert frontend["mem_limit"] == "${FRONTEND_MEM_LIMIT:-1g}"
    assert frontend["restart"] == "unless-stopped"


def test_pgadmin_is_pinned_to_an_exact_supported_release() -> None:
    """A moving tag changes the database console under a running stack.

    pgAdmin 8 is past its end of life; a bare major tag (``:9``) or ``latest``
    would upgrade it on whatever pull happens next.
    """
    image = _services()["pgadmin"]["image"]
    name, _, tag = image.partition(":")

    assert name == "dpage/pgadmin4"
    major, _, minor = tag.partition(".")
    assert major.isdigit() and int(major) >= 9, image
    assert minor.isdigit(), image


def _runtime_stage(dockerfile: Path) -> str:
    """The instructions after the last ``FROM``: what the running image is."""
    text = dockerfile.read_text()
    return text[text.rindex("\nFROM ") :]


def test_the_console_image_runs_as_an_unprivileged_user() -> None:
    """By number, so a cluster that requires a non-root user can verify it."""
    runtime = _runtime_stage(FRONTEND_DOCKERFILE)
    users = [line.split()[1] for line in runtime.splitlines() if line.startswith("USER ")]

    assert users == ["1000:1000"]
    # Its one writable directory, owned before the switch.
    assert runtime.index("chown 1000:1000 .next/cache") < runtime.index("USER 1000:1000")
    assert runtime.index("USER 1000:1000") < runtime.index('CMD ["node", "server.js"]')


def test_no_service_but_pgadmin_gains_privileges_through_exec() -> None:
    """pgAdmin binds port 80 through a file capability; no-new-privileges would refuse it."""
    services = _services()

    for name in ("backend", "frontend", "postgres"):
        assert "no-new-privileges:true" in services[name].get("security_opt", []), name
    assert "security_opt" not in services["pgadmin"]


def test_the_console_and_gateway_drop_the_capabilities_they_do_not_use() -> None:
    services = _services()

    assert services["frontend"]["cap_drop"] == ["ALL"]
    assert "cap_add" not in services["frontend"]
    # Root in the gateway keeps exactly what reading and writing its mounted
    # host directories needs, whoever owns the files in them.
    assert services["backend"]["cap_drop"] == ["ALL"]
    assert sorted(services["backend"]["cap_add"]) == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID"]


def test_pgadmin_is_reachable_only_from_the_console_and_its_database() -> None:
    """pgAdmin may serve without a login; the backend faces the internet."""
    services = _services()

    assert services["pgadmin"]["networks"] == ["pgadmin"]
    on_pgadmin_network = {
        name for name, svc in services.items() if "pgadmin" in svc.get("networks", [])
    }
    assert on_pgadmin_network == {"frontend", "pgadmin", "postgres"}
    assert "pgadmin" not in services["backend"]["networks"]


PGADMIN_PORT_OVERLAY = REPO / "deploy" / "docker" / "docker-compose.pgadmin-port.yml"


def test_pgadmin_publishes_no_host_port_unless_asked() -> None:
    """A host port skips the console's admin check; every local account can reach it.

    pgAdmin carries a saved superuser connection, so that port handed the
    database, and every secret in app_config, to anyone on the host.
    """
    pgadmin = _services()["pgadmin"]

    assert "ports" not in pgadmin
    assert "expose" not in pgadmin
    assert "PGADMIN_PORT" not in COMPOSE.read_text()
    # The opt-in publishes on loopback only.
    assert _services(PGADMIN_PORT_OVERLAY) == {
        "pgadmin": {"ports": ["127.0.0.1:${PGADMIN_PORT:-5050}:80"]}
    }


def _compose_command(*make_args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in {"DISTRIBUTION", "PGADMIN_PUBLISH"}}
    proc = subprocess.run(
        ["make", "--no-print-directory", "-n", "ps", "DISTRIBUTION=none", *make_args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = [line for line in proc.stdout.splitlines() if "docker compose" in line]
    return line


def test_the_makefile_publishes_pgadmin_only_when_asked() -> None:
    overlay = "-f deploy/docker/docker-compose.pgadmin-port.yml"

    assert overlay not in _compose_command()
    assert overlay not in _compose_command("PGADMIN_PUBLISH=0")
    assert overlay in _compose_command("PGADMIN_PUBLISH=1")


def test_pgadmin_asks_for_its_own_login_with_no_fallback_password() -> None:
    """The fallbacks were desktop mode, which has no login, and the password ``admin``.

    A required ``${PGADMIN_PASSWORD:?}`` would stop every Compose command,
    pgAdmin's profile in use or not, so the password has no default instead:
    pgAdmin itself refuses to set up a volume without one.
    """
    environment = _services()["pgadmin"]["environment"]

    assert environment["PGADMIN_CONFIG_SERVER_MODE"] == "${PGADMIN_CONFIG_SERVER_MODE:-True}"
    assert environment["PGADMIN_DEFAULT_PASSWORD"] == "${PGADMIN_PASSWORD-}"
    assert "PGADMIN_CONFIG_SERVER_MODE=True" in (REPO / ".env.example").read_text().splitlines()
