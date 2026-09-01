"""The legacy `/agents` rewrite remains as a compatibility bridge.

Next resolves `rewrites()` at **build time** and writes the result into
`.next/routes-manifest.json`. Existing branded images may still carry those
rules, while neutral images leave them empty and use the runtime route handler.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"
DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile.frontend"

AGENT_URLS = ("AGENT_WEB_INTERNAL_URL", "AGENT_CONTROL_PLANE_INTERNAL_URL")
SITE_ASSETS_DIR = "SITE_ASSETS_DIR"


def _frontend() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["frontend"]


def test_the_agent_urls_are_build_args() -> None:
    """Legacy branded builds keep routing during the gradual cutover."""
    args = _frontend()["build"]["args"]
    for name in AGENT_URLS:
        assert name in args, (
            f"{name} is not a build arg, so a legacy branded image cannot "
            "compile its compatibility rewrite during the gradual cutover"
        )


def test_the_agent_urls_are_runtime_environment() -> None:
    """Neutral images route from server-only values supplied at startup."""
    environment = _frontend()["environment"]
    for name in AGENT_URLS:
        assert environment[name] == f"${{{name}-}}"


def test_backend_internal_url_is_runtime_environment() -> None:
    """Server-first branding lookup follows the deployment's backend target."""
    environment = _frontend()["environment"]
    assert environment["BACKEND_INTERNAL_URL"] == "${BACKEND_INTERNAL_URL-http://backend:8080}"


def test_site_assets_directory_is_runtime_only() -> None:
    """A deployment mounts branding images without compiling their path in."""
    frontend = _frontend()
    assert frontend["environment"][SITE_ASSETS_DIR] == f"${{{SITE_ASSETS_DIR}-}}"
    assert SITE_ASSETS_DIR not in frontend["build"]["args"]


def test_the_dockerfile_carries_them_into_the_build() -> None:
    """A build arg compose passes and the Dockerfile never declares is dropped."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    for name in AGENT_URLS:
        assert f"ARG {name}=" in text, f"{name} is not declared as an ARG"
        assert f"ENV {name}=${name}" in text, (
            f"{name} is declared but never promoted to ENV, so `next build` does not see it"
        )


def test_they_default_to_empty_so_other_deployments_are_untouched() -> None:
    """Empty build args leave the runtime route authoritative by default."""
    args = _frontend()["build"]["args"]
    for name in AGENT_URLS:
        assert args[name] == f"${{{name}-}}", f"{name} must default to empty; got {args[name]!r}"
