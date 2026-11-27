"""The console's `/agents` targets and branding directory are runtime settings.

The `/agents` route handler reads its targets on every request, so upstream
Compose supplies them to the running container and never to the build.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "docker" / "docker-compose.yml"

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


def test_the_public_agent_link_is_runtime_only() -> None:
    """A standalone agent can move without rebuilding the neutral console."""
    frontend = _frontend()
    assert frontend["environment"]["AGENT_PUBLIC_URL"] == "${AGENT_PUBLIC_URL-}"
    assert "AGENT_PUBLIC_URL" not in frontend["build"]["args"]


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
