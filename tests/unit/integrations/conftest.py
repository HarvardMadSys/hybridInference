"""Load the Tencent Cloud integration the way a deployment does, in isolation."""

from __future__ import annotations

import pytest

from serving import extensions
from serving.config import app_config, app_config_registry as registry
from serving.config.settings import get_settings
from serving.utils import delivery


@pytest.fixture(autouse=True)
def tencent_cloud_loaded(monkeypatch):
    monkeypatch.setattr(registry, "_EXTENSION_ENTRIES", {})
    monkeypatch.setattr(delivery, "_email_providers", {})
    monkeypatch.setattr(delivery, "_sms_providers", {})
    monkeypatch.setattr(extensions, "_loaded_modules", set())
    monkeypatch.setenv("BACKEND_EXTENSIONS", "serving.integrations.tencent_cloud")
    registry.invalidate_config_registry()
    app_config.reset_state()
    extensions.load_backend_extensions()
    yield
    app_config.reset_state()
    registry.invalidate_config_registry()


@pytest.fixture
def clean_config(monkeypatch, tmp_path):
    """No registered setting in the environment, and empty configuration files."""
    from serving.integrations.tencent_cloud import config
    from serving.utils import email, sms

    for entry in registry.static_entries():
        monkeypatch.delenv(entry.key, raising=False)
    # Runtime settings are not static entries, and the session-wide
    # tests/servers environment sets some of them (signup without email
    # verification) for every later test in the same worker.
    for key in registry.RUNTIME_SETTING_NAMES:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("DISTRIBUTION_CONFIG_PATH", raising=False)
    (tmp_path / "models.yaml").write_text("models: []\n")
    monkeypatch.setenv("MODELS_CONFIG_PATH", str(tmp_path / "models.yaml"))
    monkeypatch.setenv("ROUTING_CONFIG_PATH", str(tmp_path / "routing.yaml"))
    monkeypatch.setenv("ALERTS_CONFIG_PATH", str(tmp_path / "alerts.yaml"))
    get_settings.cache_clear()
    current = get_settings()
    monkeypatch.setattr(email, "settings", current)
    monkeypatch.setattr(sms, "settings", current)
    monkeypatch.setattr(config, "core_settings", current)
    yield
    get_settings.cache_clear()
