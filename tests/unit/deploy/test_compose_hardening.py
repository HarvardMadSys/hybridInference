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
