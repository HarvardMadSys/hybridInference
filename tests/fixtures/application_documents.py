"""Prepare JSON settings for tests that retain legacy documents as input fixtures.

This is test setup, not a runtime compatibility path: the gateway receives only
the JSON settings, and the no-file-fallback tests cover that boundary directly.
"""

from __future__ import annotations

import json

import yaml


def seed_models(monkeypatch, path):
    """Seed a catalog from a fixture file before starting a test gateway."""
    from serving.config.documents import separate_secrets

    document, secrets = separate_secrets(yaml.safe_load(path.read_text()) or {}, "test")
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MODEL_CATALOG", json.dumps(document))


def seed_site(monkeypatch, path):
    """Seed a public site document and its optional branding fixture."""
    if not path.exists():
        monkeypatch.setenv("SITE_CONFIGURATION", "invalid")
        return
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        monkeypatch.setenv("SITE_CONFIGURATION", "invalid")
        return
    if isinstance(document, dict):
        document.pop("paths", None)
        site = document.get("site") or {}
        branding = site.pop("branding", "")
        if branding:
            monkeypatch.setenv(
                "SITE_BRANDING", json.dumps(yaml.safe_load((path.parent / branding).read_text()))
            )
    monkeypatch.setenv("SITE_CONFIGURATION", json.dumps(document))
