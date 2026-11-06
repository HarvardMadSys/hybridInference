"""What the base Compose file grants each container, and what it holds back.

Read from the YAML rather than rendered, so the checks need neither Docker nor
an env file; ``test_compose_bindings.py`` renders the file when the Compose CLI
is installed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
FRONTEND_DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile.frontend"


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


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
