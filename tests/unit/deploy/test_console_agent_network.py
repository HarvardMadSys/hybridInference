"""The console can only proxy `/agents` on a network it is actually on.

`#1206` gave the console rewrites pointing at ``http://web:3000`` and
``http://control-plane:8000`` -- Docker DNS names belonging to the standalone
cloud agent's stack. A name resolves only on a shared network, and the console
was on ``hybridinference`` alone, so staging answered **500** for `/agents`
while every check that looks at the console alone stayed green: the image
built, the rewrite was present, the target simply did not exist.

The attachment therefore lives in an overlay that is opt-in, and the two
failure modes it sits between are what these tests pin:

* named in the base file, an ``external`` network that no other deployment has
  fails *every* compose command for them -- not the feature, the whole stack;
* left out of the deploy script, the overlay exists and is never applied, which
  is the 500 above with an extra file in the tree to suggest otherwise.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
BASE = REPO / "deploy" / "docker" / "docker-compose.yml"
OVERLAY = REPO / "deploy" / "docker" / "docker-compose.cloud-agent.yml"
MAKEFILE = REPO / "Makefile"


def test_the_overlay_puts_the_console_on_both_networks() -> None:
    """Compose replaces this list rather than merging it.

    Naming only ``cloud-agent`` here would take the console off the network it
    reaches the backend on -- trading a broken `/agents` for a broken
    everything.
    """
    overlay = yaml.safe_load(OVERLAY.read_text())
    networks = overlay["services"]["frontend"]["networks"]

    assert "cloud-agent" in networks
    assert "hybridinference" in networks


def test_the_agent_network_is_external_and_named_by_variable() -> None:
    """It belongs to the other repository's stack, which names it.

    Both sides default to ``cloud-agent``; if they ever disagree the symptom is
    the same 500, so the name must come from one variable rather than a literal
    repeated in two repositories.
    """
    overlay = yaml.safe_load(OVERLAY.read_text())
    network = overlay["networks"]["cloud-agent"]

    assert network["external"] is True
    assert network["name"] == "${AGENT_NETWORK_NAME:-cloud-agent}"


def test_the_base_stack_never_mentions_it() -> None:
    """A fresh clone has no such network, and `external` would be fatal."""
    base = yaml.safe_load(BASE.read_text())

    assert "cloud-agent" not in (base.get("networks") or {})
    assert base["services"]["frontend"]["networks"] == ["hybridinference"]


def test_the_makefile_adds_the_overlay_only_when_asked() -> None:
    makefile = MAKEFILE.read_text()

    assert "ifeq ($(CLOUD_AGENT_NETWORK),1)" in makefile
    assert "COMPOSE_FILE_ARGS += -f deploy/docker/docker-compose.cloud-agent.yml" in makefile
