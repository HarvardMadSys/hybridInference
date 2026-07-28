"""The compose stack must name no deployment until one supplies its identity.

``docker compose up`` on a fresh clone is how most people first run this
project. If the compose file carried one site's name, links, CORS origins and
analytics ids as defaults, that clone would come up impersonating the site --
emailing from its domain, reporting traffic into its analytics account, and
telling users to call its API. So the defaults here name nobody, and the
values that make a deployment *itself* live in that distribution's overlay,
fed to ``docker compose --env-file`` by its deploy script.

That arrangement only holds while three things stay true, and each is easy to
undo by accident, so each gets a test: the compose defaults stay neutral, the
overlay still supplies everything they gave up, and the deploy scripts still
read the overlay. No Docker is required -- the files are parsed directly.
"""

from __future__ import annotations

import re
import sys
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

sys.path.insert(0, str(REPO / "apps" / "backend"))

# ${VAR-default} and ${VAR:-default}
_INTERPOLATION = re.compile(r"^\$\{(?P<var>[A-Z_]+):?-(?P<default>.*)\}$", re.DOTALL)

# Anything that would identify one particular deployment. Matched
# case-insensitively against every compose default.
_DEPLOYMENT_MARKERS = (
    "freeinference",
    "junchengyang",
    "madsys",
    "harvard",
    "13224568",  # the site's Statcounter project
    "2d8ab84a",  # ...and its security key
)

# The upstream project's own repository. It identifies the software, not a
# deployment of it, so it is the one value allowed to survive as a default.
_PROJECT_REPO_VAR = "NEXT_PUBLIC_GITHUB_URL"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _compose_defaults() -> dict[str, str]:
    """Every backend env var and frontend build arg, mapped to its default."""
    compose = _compose()
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
    """Every key the FreeInference overlay sets, across all of its env files."""
    values: dict[str, str] = {}
    for path in sorted(OVERLAY.glob("*.env")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def test_no_compose_default_names_a_deployment() -> None:
    """A clone that supplies nothing must not come up as somebody's site."""
    offenders = {
        name: default
        for name, default in _compose_defaults().items()
        if name != _PROJECT_REPO_VAR
        and any(marker in default.lower() for marker in _DEPLOYMENT_MARKERS)
    }
    assert not offenders, (
        "these compose defaults would brand a fresh clone as someone else's "
        f"deployment; move the value into a distribution overlay: {offenders}"
    )


@pytest.mark.parametrize(
    ("var", "expected"),
    [
        ("SITE_NAME", "HybridInference"),
        ("SITE_PUBLIC_BASE_URL", ""),
        ("SITE_DOCS_URL", ""),
        ("SITE_SUPPORT_EMAIL", ""),
        ("SMTP_FROM_EMAIL", "noreply@localhost"),
        ("SMTP_FROM_NAME", "HybridInference"),
        ("BASE_URL", ""),
        ("FRONTEND_URL", "http://localhost:3001"),
    ],
)
def test_compose_neutral_default_matches_the_code_default(
    var: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compose and the code must agree, or the two neutral defaults drift.

    Compose has to name each variable to pass it into the container, so the
    neutral value ends up written twice. This pins them together.
    """
    from serving.config.settings import Settings
    from serving.config.site_identity import NEUTRAL_DEFAULT

    # The subject is the *default*, so the ambient environment must not
    # answer for it -- another test setting BASE_URL would otherwise decide
    # this one's verdict.
    monkeypatch.delenv(var, raising=False)
    settings = Settings()
    code_default = {
        "SITE_NAME": NEUTRAL_DEFAULT.name,
        "SITE_PUBLIC_BASE_URL": NEUTRAL_DEFAULT.public_base_url,
        "SITE_DOCS_URL": NEUTRAL_DEFAULT.docs_url,
        "SITE_SUPPORT_EMAIL": NEUTRAL_DEFAULT.support_email,
        "SMTP_FROM_EMAIL": settings.smtp_from_email,
        "SMTP_FROM_NAME": settings.smtp_from_name,
        "BASE_URL": settings.base_url,
        "FRONTEND_URL": settings.frontend_url,
    }[var]

    assert _compose_defaults()[var] == expected
    assert code_default == expected


def test_compose_cors_default_admits_only_local_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The site's public origins belong to the site, not to every clone."""
    from serving.config.settings import Settings

    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    origins = _compose_defaults()["CORS_ALLOWED_ORIGINS"].split(",")
    assert origins == Settings().cors_allowed_origins
    assert all("localhost" in o or "127.0.0.1" in o for o in origins)


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
