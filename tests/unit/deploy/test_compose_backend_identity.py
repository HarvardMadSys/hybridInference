"""The deployment must supply the identity the code no longer defaults to.

Emails, links, CORS and OpenRouter attribution used to carry FreeInference as
code defaults. Those defaults are now neutral, and
``deploy/docker/docker-compose.yml`` pins the real values for the backend.
These pins are the reason a neutral upstream does not rebrand the running
site, so losing one would silently change production output — hence a test
that reads the compose file directly (no Docker required).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker" / "docker-compose.yml"

# ${VAR-default}
_INTERPOLATION = re.compile(r"^\$\{[A-Z_]+-(?P<default>.*)\}$", re.DOTALL)


def _backend_env_default(name: str) -> str:
    env = yaml.safe_load(COMPOSE.read_text())["services"]["backend"]["environment"]
    raw = str(env[name])
    match = _INTERPOLATION.match(raw)
    assert match, f"{name} should stay overridable as ${{{name}-<default>}}, got {raw[:60]!r}"
    return match.group("default")


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
    ],
)
def test_backend_identity_is_pinned_by_the_deployment(var: str, expected: str) -> None:
    """Each value the upstream default gave up is supplied by the deployment."""
    assert _backend_env_default(var) == expected


def test_cors_pin_still_admits_the_public_site() -> None:
    """The site's own origins live in the deployment, not in code defaults."""
    origins = _backend_env_default("CORS_ALLOWED_ORIGINS").split(",")
    assert "https://freeinference.org" in origins
    assert "https://staging-internal.freeinference.org" in origins
    # Local development origins stay in the code default, so they must not be
    # the only thing this pin contributes.
    assert len(origins) > 8
