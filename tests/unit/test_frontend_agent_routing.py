"""The runtime `/agents` route is authoritative in upstream Compose builds.

The Dockerfile still accepts explicit agent build args as a compatibility
bridge for pre-W7 downstream pipelines. Upstream Compose must not copy its
runtime targets into those args, because a compiled ``beforeFiles`` rewrite
would outrank the runtime route handler.
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


def test_the_agent_urls_are_not_compose_build_args() -> None:
    """A source build cannot accidentally bake this deployment's targets."""
    args = _frontend()["build"]["args"]
    for name in AGENT_URLS:
        assert name not in args, f"{name} must remain runtime-only in upstream Compose"


def test_the_agent_urls_are_runtime_environment() -> None:
    """Neutral images route from server-only values supplied at startup."""
    environment = _frontend()["environment"]
    for name in AGENT_URLS:
        assert environment[name] == f"${{{name}-}}"


def test_backend_internal_url_is_one_build_time_contract() -> None:
    """Rewrites and server fetches cannot be retargeted independently."""
    frontend = _frontend()
    assert frontend["build"]["args"]["BACKEND_INTERNAL_URL"] == (
        "${BACKEND_INTERNAL_URL-http://backend:8080}"
    )
    assert "BACKEND_INTERNAL_URL" not in frontend["environment"]


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
