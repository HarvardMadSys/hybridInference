"""The `/agents` rewrite is a build arg, and nothing at runtime says otherwise.

Next resolves `rewrites()` at **build time** and writes the result into
`.next/routes-manifest.json`. Supplying the URLs only as container environment
therefore routes nothing — and the failure is invisible from every angle an
operator checks: the variables are present in `docker inspect`, the container
is healthy, `/agents` returns 200, and it is the old app. The manifest is the
only place the truth lives, and nobody looks there.

Caught exactly that way on staging: `beforeFiles: []` in a container whose
environment held both URLs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile.frontend"

AGENT_URLS = ("AGENT_WEB_INTERNAL_URL", "AGENT_CONTROL_PLANE_INTERNAL_URL")


def _frontend() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["frontend"]


def test_the_agent_urls_are_build_args() -> None:
    """Runtime-only, they are read by nothing and the old pages keep serving."""
    args = _frontend()["build"]["args"]
    for name in AGENT_URLS:
        assert name in args, (
            f"{name} is not a build arg. Next bakes rewrites into "
            "routes-manifest.json at build time, so a runtime-only value "
            "routes nothing while looking correctly set"
        )


def test_the_dockerfile_carries_them_into_the_build() -> None:
    """A build arg compose passes and the Dockerfile never declares is dropped."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    for name in AGENT_URLS:
        assert f"ARG {name}=" in text, f"{name} is not declared as an ARG"
        assert f"ENV {name}=${name}" in text, (
            f"{name} is declared but never promoted to ENV, so `next build` does not see it"
        )


def test_they_default_to_empty_so_other_deployments_are_untouched() -> None:
    """Unset must mean "keep this app's own /agents pages", not "route to ''"."""
    args = _frontend()["build"]["args"]
    for name in AGENT_URLS:
        assert args[name] == f"${{{name}-}}", f"{name} must default to empty; got {args[name]!r}"
