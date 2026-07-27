"""Tests for the backend site-identity resolution chain."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import serving.config.distribution as distribution_module
import serving.config.settings as settings_module
from serving.config.site_identity import LEGACY_DEFAULT, get_site_identity
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


def test_defaults_are_the_legacy_freeinference_identity():
    # Behavior freeze: an unconfigured deployment renders exactly what it
    # rendered before this module existed. The neutral flip is a later,
    # deliberate PR that changes LEGACY_DEFAULT and the frozen assertions.
    identity = get_site_identity()
    assert identity == LEGACY_DEFAULT
    assert identity.name == "FreeInference"
    assert identity.support_email == "admin@freeinference.org"


def test_env_overrides_win(monkeypatch):
    monkeypatch.setenv("SITE_NAME", "AcmeLLM")
    monkeypatch.setenv("SITE_DOCS_URL", "https://docs.acme.example")
    identity = get_site_identity()
    assert identity.name == "AcmeLLM"
    assert identity.docs_url == "https://docs.acme.example"
    # Untouched fields keep the legacy defaults.
    assert identity.support_email == LEGACY_DEFAULT.support_email


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
    assert get_site_identity() == LEGACY_DEFAULT


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
