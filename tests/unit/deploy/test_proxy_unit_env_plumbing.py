"""The idle proxies must read the gateway's key from .env, and nothing else.

Two failures took the DGX Spark proxy down together. Its systemd unit was never
installed, so a host reboot ended the process; and when it was restarted by hand
the operator lifted ``LOCAL_API_KEY`` out of ``.env`` with ``grep | cut -d=``,
which keeps the single quotes the file writes around the value, so the proxy
compared a quoted key against the gateway's unquoted one and 401'd every request
for the model it serves. Nothing alerted.

The unit could not have supplied the key either: it set no ``LOCAL_API_KEY`` at
all, so a systemd-managed proxy could only ever use the default hardcoded in the
proxy source -- correct until the day that key is rotated, then silently wrong.
So the units now read the repo ``.env``, the same file the gateway signs its
requests from, and systemd -- which does apply POSIX shell quoting -- strips the
quotes the shell one-liner kept.

Reading a whole gateway .env into a proxy unit costs something, though, and this
is the part that is easy to get wrong later: ``EnvironmentFile=`` outranks
``Environment=`` unconditionally, so any key in .env silently wins over the
unit's own setting and no amount of reordering the directives changes that.
``MODELS_CONFIG`` is exactly such a key -- a legacy gateway alias naming a YAML
registry, and the proxies' name for their own JSON one -- so the units drop it
with ``UnsetEnvironment=``, which systemd applies last of all. The rest of what
the units pin is safe only while .env never grows a key of the same name, which
is what the last test here watches for.

No systemd is required: the files are parsed directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SYSTEMD = REPO / "deploy" / "systemd"
ENV_EXAMPLE = REPO / ".env.example"
SETTINGS = REPO / "apps" / "backend" / "serving" / "config" / "settings.py"
INSTALLER = REPO / "ops" / "local_deployment_proxy" / "install.sh"

# The units this PR wired up. h200_idle_proxy.service has the same gap and is
# left for its own change; adding it here is how that change gets noticed.
PROXY_UNITS = ("spark_idle_proxy.service", "local_deployment_proxy.service")

# Assignments in .env.example, including the commented-out ones -- a documented
# knob an operator uncomments lands in a real .env just the same.
_ENV_ASSIGNMENT = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def _directive(text: str, key: str) -> list[str]:
    """Values of every ``key=`` directive in a unit, comments excluded."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            out.append(value.strip())
    return out


def _pinned_env_keys(text: str) -> set[str]:
    """Variable names the unit sets with ``Environment=``."""
    return {v.partition("=")[0].strip().strip('"') for v in _directive(text, "Environment")}


@pytest.mark.parametrize("unit", PROXY_UNITS)
def test_proxy_unit_reads_the_repo_env(unit: str) -> None:
    """Without this the key can only ever be the one hardcoded in the source."""
    files = _directive(_unit(unit), "EnvironmentFile")
    assert "-__REPO_ROOT__/.env" in files, (
        f"{unit} must read the repo .env so LOCAL_API_KEY matches what the gateway "
        f"sends; the leading '-' keeps a box without one startable. Found: {files}"
    )


@pytest.mark.parametrize("unit", PROXY_UNITS)
def test_proxy_unit_drops_the_gateways_models_config(unit: str) -> None:
    """A gateway MODELS_CONFIG names YAML; these proxies json.load() the path."""
    text = _unit(unit)
    unset = {v.strip() for value in _directive(text, "UnsetEnvironment") for v in value.split()}
    assert "MODELS_CONFIG" in unset, (
        f"{unit} reads a gateway .env, where MODELS_CONFIG may name a YAML registry. "
        "EnvironmentFile= outranks Environment= whatever the order, so only "
        "UnsetEnvironment= -- applied last -- can keep it out."
    )
    assert "MODELS_CONFIG" not in _pinned_env_keys(text), (
        f"{unit} both sets and unsets MODELS_CONFIG; UnsetEnvironment= runs last, "
        "so the Environment= line is dead weight that reads as if it worked."
    )


def test_models_config_is_still_a_gateway_variable() -> None:
    """The premise for unsetting it: drop the alias and the units can stop."""
    assert "MODELS_CONFIG" in SETTINGS.read_text(encoding="utf-8"), (
        "the proxy units unset MODELS_CONFIG because the gateway still accepts it "
        f"as a legacy alias. If {SETTINGS.name} no longer names it, that collision "
        "is gone and the UnsetEnvironment= lines can go with it."
    )


def test_env_example_declares_nothing_a_proxy_unit_pins() -> None:
    """.env.example is the template every real .env grows from.

    A key added there that a proxy unit also sets with ``Environment=`` would
    take over the moment the unit reads the file, because environment files
    outrank ``Environment=`` unconditionally. The unit would still read as
    though it decided the value. Nothing in the unit can defend against this, so
    the defence is here: keep the two namespaces disjoint.
    """
    declared = set(_ENV_ASSIGNMENT.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    collisions = {
        unit: sorted(_pinned_env_keys(_unit(unit)) & declared)
        for unit in PROXY_UNITS
        if _pinned_env_keys(_unit(unit)) & declared
    }
    assert not collisions, (
        "these .env.example keys would override the proxy unit setting of the same "
        f"name, silently: {collisions}. Rename the gateway variable, or move the "
        "unit's value out of the environment."
    )


def test_installer_passes_the_key_to_the_proxy_not_the_tunnel() -> None:
    """The port drop-in it already wrote went to the tunnel unit's directory."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'PROXY_DROPIN="${SYSTEMD_DST}/${PROXY_UNIT}.d"' in text, (
        "the installer's only drop-in went to the tunnel unit, so it could not set "
        "proxy-process env at all; LOCAL_API_KEY has to land on the proxy unit."
    )
    key_block = text.partition("PROXY_DROPIN=")[2]
    assert "LOCAL_API_KEY" in key_block, "the proxy drop-in must carry LOCAL_API_KEY"
    assert "rm -f" in key_block, (
        "omitting LOCAL_API_KEY must clear a drop-in from an earlier run, or a "
        "rotated-away key outlives the rotation on boxes with no .env"
    )
