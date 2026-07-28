"""Tests for the backend site-identity resolution chain."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import serving.config.distribution as distribution_module
import serving.config.settings as settings_module
from serving.config.site_identity import NEUTRAL_DEFAULT, get_site_identity
from serving.rag.pipeline import system_prompt
from serving.utils.email import render_broadcast_template, render_markdown_email

_ENV_KEYS = ("SITE_NAME", "SITE_PUBLIC_BASE_URL", "SITE_DOCS_URL", "SITE_SUPPORT_EMAIL")


@pytest.fixture(autouse=True)
def _clean_site_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(distribution_config_mode=mode),
    )


def _force_manifest(monkeypatch, *, display_name: str, base_url: str, support: str) -> None:
    config = SimpleNamespace(
        distribution=SimpleNamespace(display_name=display_name),
        site=SimpleNamespace(public_base_url=base_url, support_email=support),
    )
    monkeypatch.setattr(distribution_module, "get_distribution_config", lambda: config)


def test_defaults_name_no_distribution():
    # An unconfigured deployment is nobody's: it carries the product name and
    # no borrowed URLs or contact address. FreeInference's identity arrives
    # through SITE_* (pinned in docker-compose), not from this module.
    identity = get_site_identity()
    assert identity == NEUTRAL_DEFAULT
    assert identity.name == "HybridInference"
    assert identity.public_base_url == ""
    assert identity.support_email == ""
    assert "freeinference" not in str(identity).lower()


def test_env_overrides_win(monkeypatch):
    monkeypatch.setenv("SITE_NAME", "AcmeLLM")
    monkeypatch.setenv("SITE_DOCS_URL", "https://docs.acme.example")
    identity = get_site_identity()
    assert identity.name == "AcmeLLM"
    assert identity.docs_url == "https://docs.acme.example"
    # Untouched fields keep the legacy defaults.
    assert identity.support_email == NEUTRAL_DEFAULT.support_email


def test_active_manifest_supplies_identity(monkeypatch):
    _force_mode(monkeypatch, "active")
    _force_manifest(
        monkeypatch,
        display_name="Acme",
        base_url="https://acme.example",
        support="help@acme.example",
    )
    identity = get_site_identity()
    assert identity.name == "Acme"
    assert identity.public_base_url == "https://acme.example"
    assert identity.support_email == "help@acme.example"


def test_dark_mode_ignores_manifest(monkeypatch):
    _force_mode(monkeypatch, "dark")
    _force_manifest(
        monkeypatch,
        display_name="Acme",
        base_url="https://acme.example",
        support="help@acme.example",
    )
    assert get_site_identity() == NEUTRAL_DEFAULT


def test_env_beats_active_manifest(monkeypatch):
    _force_mode(monkeypatch, "active")
    _force_manifest(
        monkeypatch,
        display_name="Acme",
        base_url="https://acme.example",
        support="help@acme.example",
    )
    monkeypatch.setenv("SITE_NAME", "EnvName")
    assert get_site_identity().name == "EnvName"


def test_email_content_follows_identity(monkeypatch):
    monkeypatch.setenv("SITE_NAME", "Acme")
    html, _text = render_markdown_email("hello")
    assert "active Acme account." in html
    rendered = render_broadcast_template("quota_change", {"new_quota": "$5"})
    assert rendered["subject"] == "Your Acme quota has been updated"
    assert "active Acme account." in rendered["body_html"]


def test_broadcast_template_vars_cannot_spoof_site_name(monkeypatch):
    monkeypatch.setenv("SITE_NAME", "Acme")
    rendered = render_broadcast_template("quota_change", {"new_quota": "$5", "site_name": "Evil"})
    assert "Evil" not in rendered["body_html"]


def test_rag_system_prompt_follows_identity(monkeypatch):
    monkeypatch.setenv("SITE_NAME", "Acme")
    monkeypatch.setenv("SITE_DOCS_URL", "https://docs.acme.example")
    prompt = system_prompt()
    assert prompt.startswith("You are the Acme documentation assistant.")
    assert "https://docs.acme.example" in prompt
    assert "freeinference" not in prompt.lower()


def test_openrouter_attribution_follows_a_configured_identity(monkeypatch):
    """A configured deployment attributes as itself, headers included."""
    from serving.adapters.base import ModelConfig
    from serving.adapters.openrouter import OpenRouterAdapter

    monkeypatch.setenv("SITE_NAME", "Acme Inference")
    monkeypatch.setenv("SITE_PUBLIC_BASE_URL", "https://acme.example")

    adapter = OpenRouterAdapter(
        ModelConfig(id="m", name="m", provider="openrouter", base_url="https://x/v1", api_key="k")
    )
    headers = adapter._build_headers()

    assert headers["HTTP-Referer"] == "https://acme.example"
    assert headers["X-Title"] == "Acme Inference"


def test_quota_contact_follows_a_configured_identity(monkeypatch):
    """A configured deployment names its own support address in quota errors."""
    from serving.servers.auth import _quota_contact

    monkeypatch.setenv("SITE_SUPPORT_EMAIL", "help@acme.example")
    assert _quota_contact() == "help@acme.example"


def test_usage_insights_targets_the_configured_gateway(monkeypatch):
    """Analysis calls follow the operator's own public base URL."""
    from serving.servers.routers.admin.usage_insights import _analysis_base_url

    monkeypatch.setenv("SITE_PUBLIC_BASE_URL", "https://acme.example")
    assert _analysis_base_url() == "https://acme.example/v1"


def test_no_module_hardcodes_openrouter_attribution():
    """Every OpenRouter call must attribute to whoever is running the gateway.

    The adapter learned this first; an admin-side endpoint probe kept its own
    literal headers and so reported a third party's traffic into this site's
    OpenRouter account. The rule is easier to keep as a rule than to remember
    at each new call site, so it is asserted over the source.
    """
    import re
    from pathlib import Path

    backend = Path(__file__).resolve().parents[3] / "apps" / "backend"
    helper = backend / "serving" / "adapters" / "openrouter.py"

    literal = re.compile(r'["\'](?:HTTP-Referer|X-Title)["\']\s*:\s*["\'][^"\']+["\']')
    offenders = [
        f"{path.relative_to(backend)}:{i}"
        for path in backend.rglob("*.py")
        for i, line in enumerate(path.read_text().splitlines(), start=1)
        if literal.search(line) and path != helper
    ]
    assert not offenders, (
        "these send a fixed OpenRouter attribution instead of the deployment's "
        f"own; call openrouter_attribution_headers() instead: {offenders}"
    )
