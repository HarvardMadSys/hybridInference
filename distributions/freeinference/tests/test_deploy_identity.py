"""What makes the running stack this site, pinned where the values live.

Upstream ships compose defaults that name no deployment and a test that keeps
them that way. These are the other half: the values this overlay supplies, and
the wiring that carries them. They live here because they read this overlay and
the deploy scripts, neither of which travels — an upstream test that reads them
would break the moment the export drops them, which is how the exported
repository ended up unable to run its own suite.

Each of these fails silently in production if lost: the console rebuilds
unbranded, alerts fall back to built-in thresholds, routing loses its endpoint
map. That is why they are assertions rather than documentation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
OVERLAY = REPO / "distributions" / "freeinference" / "deploy"
DEPLOY_SCRIPTS = (
    REPO / "ops" / "deploy" / "deploy_production.sh",
    REPO / "ops" / "deploy" / "deploy_staging.sh",
)

# ${VAR-default} and ${VAR:-default}
_INTERPOLATION = re.compile(r"^\$\{(?P<var>[A-Z_]+):?-(?P<default>.*)\}$", re.DOTALL)


def _compose_defaults() -> dict[str, str]:
    """Every backend env var and frontend build arg, mapped to its default."""
    compose = yaml.safe_load(COMPOSE.read_text())
    raw: dict[str, str] = {}
    raw.update(compose["services"]["backend"]["environment"])
    raw.update(compose["services"]["frontend"]["build"]["args"])

    defaults: dict[str, str] = {}
    for name, value in raw.items():
        match = _INTERPOLATION.match(str(value))
        if match:
            defaults[name] = match.group("default")
    return defaults


def _overlay_values() -> dict[str, str]:
    """Every key this overlay sets, across all of its env files."""
    values: dict[str, str] = {}
    for path in sorted(OVERLAY.glob("*.env")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


@pytest.mark.parametrize(
    ("var", "expected"),
    [
        ("SITE_NAME", "FreeInference"),
        ("SITE_PUBLIC_BASE_URL", "https://freeinference.org"),
        ("SITE_DOCS_URL", "https://doc.freeinference.org"),
        ("SITE_SUPPORT_EMAIL", "admin@freeinference.org"),
        ("SMTP_FROM_EMAIL", "noreply@freeinference.org"),
        ("SMTP_FROM_NAME", "FreeInference"),
        ("BASE_URL", "https://freeinference.org"),
        ("FRONTEND_URL", "https://freeinference.org"),
        ("NEXT_PUBLIC_API_BASE", "https://freeinference.org"),
        ("NEXT_PUBLIC_APP_NAME", "FreeInference"),
        ("NEXT_PUBLIC_SITE_HOST", "freeinference.org"),
        ("NEXT_PUBLIC_ORG_NAME", "Harvard SEAS"),
        ("NEXT_PUBLIC_DOCS_URL", "https://doc.freeinference.org/"),
        ("NEXT_PUBLIC_CONTACT_EMAIL", "admin@freeinference.org"),
        ("NEXT_PUBLIC_EXAMPLE_API_BASE", "https://freeinference.org"),
        ("NEXT_PUBLIC_STATCOUNTER_PROJECT_ID", "13224568"),
        ("NEXT_PUBLIC_STORAGE_KEY_PREFIX", "freeinference"),
        ("NEXT_PUBLIC_TEAM_IMAGE_HOST", "junchengyang.com"),
        # Losing this one is silent: the loader falls back to built-in
        # thresholds rather than erroring, so production would keep
        # alerting, on different numbers, with nothing to notice.
        ("ALERTS_CONFIG_PATH", "/app/distributions/freeinference/config/alerts.yaml"),
        # Sharper edge again: without it the deployment loses its entire
        # endpoint map and health-check settings, and still starts.
        ("ROUTING_CONFIG_PATH", "/app/distributions/freeinference/config/routing.yaml"),
    ],
)
def test_overlay_supplies_what_the_default_gave_up(var: str, expected: str) -> None:
    """Losing one of these would silently change what production serves."""
    assert _overlay_values().get(var) == expected


def test_overlay_cors_still_admits_the_public_site() -> None:
    """Dropping an origin here would break the site's own browser clients."""
    origins = _overlay_values()["CORS_ALLOWED_ORIGINS"].split(",")
    assert "https://freeinference.org" in origins
    assert "https://staging-internal.freeinference.org" in origins
    # The local development origins from the neutral default must survive too.
    assert "http://localhost:3000" in origins


@pytest.mark.parametrize(
    ("var", "expected_names"),
    [
        ("NEXT_PUBLIC_TEAM_JSON", {"Juncheng Yang", "Murphy Tian", "Haoran Ni"}),
        ("NEXT_PUBLIC_SPONSORS_JSON", {"NVIDIA", "Harvard SEAS"}),
    ],
)
def test_overlay_site_content_survives_as_json(var: str, expected_names: set[str]) -> None:
    """A value mangled in transit empties its section without any error.

    The console parses these with a fallback to an empty list, so anything
    that breaks the JSON -- a stray quote, an env-file parser folding the
    line -- removes the team or sponsors section from the live site silently.
    """
    entries = json.loads(_overlay_values()[var])
    assert isinstance(entries, list) and entries
    assert {entry["name"] for entry in entries} == expected_names


def test_overlay_states_the_deployment_data_policy() -> None:
    """A site that logs prompts has to say so; upstream says nothing for it."""
    assert "logged for research purposes" in _overlay_values()["NEXT_PUBLIC_DATA_POLICY_NOTICE"]


def test_every_overlay_key_is_actually_read_by_compose() -> None:
    """An overlay key compose never names is a value that silently does nothing."""
    unused = sorted(set(_overlay_values()) - set(_compose_defaults()))
    assert not unused, (
        "the overlay sets these, but no compose default interpolates them, so "
        f"they never reach a container: {unused}"
    )


def test_overlay_carries_no_secrets() -> None:
    """The overlay is checked in; credentials stay in the server's .env."""
    suspicious = [
        key
        for key in _overlay_values()
        if any(word in key for word in ("SECRET", "PASSWORD", "TOKEN", "API_KEY"))
        # The Statcounter "security key" is a public page-embedded id, and the
        # example env var name is a name, not a value.
        and key
        not in {"NEXT_PUBLIC_STATCOUNTER_SECURITY_KEY", "NEXT_PUBLIC_EXAMPLE_API_KEY_ENV_VAR"}
    ]
    assert not suspicious, f"secrets must live in the server's .env, not here: {suspicious}"


@pytest.mark.parametrize("script", DEPLOY_SCRIPTS, ids=lambda p: p.name)
def test_deploy_script_selects_this_site_for_the_rebuild(script: Path) -> None:
    """`make build` recompiles the console, so it needs the identity too."""
    body = script.read_text()
    assert "make build DISTRIBUTION=freeinference" in body, (
        f"{script.name} rebuilds without naming a distribution, which now ships "
        "an unbranded console"
    )


@pytest.mark.parametrize("script", DEPLOY_SCRIPTS, ids=lambda p: p.name)
def test_deploy_script_feeds_the_overlay_before_the_server_env(script: Path) -> None:
    """Without this wiring the site would deploy itself unbranded."""
    body = script.read_text()
    assert "distributions/freeinference/deploy/*.env" in body, (
        f"{script.name} no longer passes the distribution overlay to compose"
    )
    overlay_at = body.index("distributions/freeinference/deploy/*.env")
    server_env_at = body.index("COMPOSE+=(--env-file .env)")
    assert overlay_at < server_env_at, (
        "the server's .env must come last so per-host overrides still win"
    )
