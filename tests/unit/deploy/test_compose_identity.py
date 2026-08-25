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


def test_the_makefile_feeds_the_overlay_too() -> None:
    """`make build` rebuilds the console, and its identity is a build arg.

    The deploy scripts assemble their own compose command, but then call
    `make build` to rebuild images — and the Makefile builds a second command
    of its own. Missing the overlay there ships a frontend with none of the
    deployment's identity compiled in, which no test of the scripts would
    catch, and which only shows up as an unbranded production console.
    """
    makefile = (REPO / "Makefile").read_text()
    compose_line = next(
        (line for line in makefile.splitlines() if line.startswith("COMPOSE :=")), ""
    )
    assert compose_line, "Makefile no longer defines COMPOSE"
    assert "$(DISTRIBUTION_ENV_FILES)" in compose_line, (
        "make build/up must pass the distribution overlay, or a rebuild drops "
        f"the deployment's console identity: {compose_line}"
    )
    # Discovered by default, because the runbooks have operators run `make
    # build` by hand on the server: requiring a flag there would mean a routine
    # rebuild silently ships a console with no identity. The clone case is
    # handled by announcing the pick and offering DISTRIBUTION=none, and stops
    # existing once the overlay lives outside this repository.
    assert "$(DISTRIBUTION_PATH)/deploy/*.env" in makefile, (
        "the overlay must resolve through DISTRIBUTION, not a bare glob"
    )
    assert "_DISTRIBUTION_LOOKUP_ROOTS := distributions examples/distributions" in makefile
    assert "DISTRIBUTION ?=" in makefile, "DISTRIBUTION must stay overridable"
    assert "ifeq ($(DISTRIBUTION),none)" in makefile, (
        "there must be a way to ask for a stack that names no deployment"
    )
    assert "$(info Using distribution" in makefile, (
        "compiling a deployment's identity into the console must not be silent"
    )
    assert "$(LOCAL_ENV_ARGS)" in compose_line
    assert "LOCAL_ENV_ARGS := $(if $(wildcard .env),--env-file .env,)" in makefile
    assert compose_line.index("$(DISTRIBUTION_ENV_FILES)") < compose_line.index(
        "$(LOCAL_ENV_ARGS)"
    ), "the server's .env must come last so per-host overrides still win"


def test_the_makefile_accepts_extra_env_files_before_the_server_env() -> None:
    """Staging overrides must reach the Compose command used by `make build`."""
    makefile = (REPO / "Makefile").read_text()
    compose_line = next(
        (line for line in makefile.splitlines() if line.startswith("COMPOSE :=")), ""
    )
    assert "COMPOSE_EXTRA_ENV_FILES ?=" in makefile
    assert "$(wildcard $(COMPOSE_EXTRA_ENV_FILES))" in makefile
    assert "$(COMPOSE_EXTRA_ENV_ARGS)" in compose_line
    assert compose_line.index("$(DISTRIBUTION_ENV_FILES)") < compose_line.index(
        "$(COMPOSE_EXTRA_ENV_ARGS)"
    )
    assert compose_line.index("$(COMPOSE_EXTRA_ENV_ARGS)") < compose_line.index("$(LOCAL_ENV_ARGS)")


def test_the_bottom_of_the_precedence_names_files_that_exist() -> None:
    """A checkout with no distribution and no environment must still resolve.

    #1104 checked this one layer too high. Compose is not where the neutral
    case is decided: a value there is an environment variable for every
    deployment, and env returns before the manifest is read — so pinning a
    working path there fixed the clone and quietly made every deployment's
    manifest decorative. The clone's fallback belongs at the bottom of the
    precedence instead, which is this table.

    Alerts is deliberately not asserted: there is no example alerts file to
    name, and a missing one resolves to the built-in thresholds, which is the
    neutral answer.
    """
    from serving.config.distribution import _LEGACY_DEFAULTS

    for kind in ("models", "routing"):
        default = _LEGACY_DEFAULTS[kind]
        assert (REPO / default).is_file(), (
            f"the {kind} fallback is {default!r}, which this repository does not ship"
        )


def test_compose_leaves_the_config_paths_to_the_precedence() -> None:
    """Compose must supply none of them, or the manifest can never win.

    `resolve_config_path` returns on the env branch before reading the
    manifest, in active mode too. A non-empty default here is an env value for
    every deployment, so it would make DISTRIBUTION_CONFIG_MODE=active do
    nothing at all — silently, because the resolved paths would still be
    plausible files.
    """
    text = COMPOSE.read_text()
    for var in ("MODELS_CONFIG_PATH", "ROUTING_CONFIG_PATH", "ALERTS_CONFIG_PATH"):
        match = re.search(rf"^\s*{var}: \$\{{{var}-([^}}]*)\}}", text, re.M)
        assert match, f"{var} lost its neutral default in the compose file"
        assert not match.group(1).strip(), (
            f"{var} defaults to {match.group(1)!r}; a value here outranks the manifest"
        )
