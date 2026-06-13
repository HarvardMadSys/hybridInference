"""Tests for config loading and env expansion."""

from __future__ import annotations

from pathlib import Path

from status_monitor.config import load_config


def test_load_config_expands_env_and_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROBER_API_KEY", "hyi-test")
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        """
settings:
  port: 9101
  base_path: "/status-monitor/"
  probe_max_tokens: 64
gateway:
  base_url: "http://backend:8080"
  api_key: "${PROBER_API_KEY}"
  e2e_interval: 300
  probe_header: "synthetic"
registry:
  path: "/app/models.yaml"
e2e_models:
  - model_id: minimax-m2.5
    streaming: true
    probe_max_tokens: 256
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)

    assert cfg.settings.port == 9101
    assert cfg.settings.base_path == "/status-monitor"  # trailing slash stripped
    assert cfg.settings.probe_max_tokens == 64
    assert cfg.gateway.api_key == "hyi-test"
    assert cfg.gateway.e2e_interval == 300
    assert cfg.gateway.probe_header == "synthetic"
    assert cfg.registry.path == "/app/models.yaml"
    assert len(cfg.e2e_models) == 1
    assert cfg.e2e_models[0].model_id == "minimax-m2.5"
    assert cfg.e2e_models[0].probe_max_tokens == 256


def test_env_default_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        'gateway:\n  api_key: "${MISSING_KEY:-fallback}"\n',
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)

    assert cfg.gateway.api_key == "fallback"
