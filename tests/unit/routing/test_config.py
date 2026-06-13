"""Unit tests for RoutingConfig migration of legacy fields."""

from __future__ import annotations

import logging

import pytest

from routing.config import RoutingConfig, load_routing_config


@pytest.mark.unit
def test_default_router_default_is_fixed():
    cfg = RoutingConfig()
    assert cfg.default_router == "fixed"


@pytest.mark.unit
def test_legacy_routing_strategy_migrates_to_default_router():
    """routing_strategy: 'routewise' migrates to default_router='routewise'."""
    cfg = RoutingConfig.model_validate({"routing_strategy": "routewise"})
    assert cfg.default_router == "routewise"


@pytest.mark.unit
def test_explicit_default_router_wins_over_legacy():
    """When both fields are set, default_router takes precedence."""
    cfg = RoutingConfig.model_validate({"routing_strategy": "routewise", "default_router": "fixed"})
    assert cfg.default_router == "fixed"


@pytest.mark.unit
def test_legacy_fields_emit_deprecation_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        RoutingConfig.model_validate({"routing_strategy": "fixed"})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("deprecated" in m.lower() for m in msgs)


@pytest.mark.unit
def test_no_warning_when_only_default_router_used(caplog):
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        RoutingConfig.model_validate({"default_router": "fixed"})
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("deprecated" in m.lower() for m in msgs)


@pytest.mark.unit
def test_legacy_routing_parameter_still_accepted(caplog):
    """Legacy 'routing_parameter' block round-trips with a deprecation."""
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        cfg = RoutingConfig.model_validate({"routing_parameter": {"local_fraction": 0.3}})
    assert cfg.routing_parameter is not None
    assert cfg.routing_parameter.local_fraction == 0.3
    msgs = [r.getMessage() for r in caplog.records]
    assert any("deprecated" in m.lower() for m in msgs)


@pytest.mark.unit
def test_unset_endpoint_is_dropped_not_fatal(tmp_path, monkeypatch, caplog):
    """An unset ${VAR} endpoint is pruned, leaving healthy endpoints loadable."""
    monkeypatch.delenv("RC_DEPLOYMENT_URL", raising=False)
    monkeypatch.setenv("ZAI_BASE_URL", "https://zai.example")
    yaml_path = tmp_path / "routing.yaml"
    yaml_path.write_text(
        "local_deployment:\n"
        "  - endpoint: ${RC_DEPLOYMENT_URL}\n"
        "    models:\n"
        "      - orphan-model\n"
        "remote_deployment:\n"
        "  - endpoint: ${ZAI_BASE_URL}\n"
        "    models:\n"
        "      - glm-4.7\n"
    )
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        cfg = load_routing_config(yaml_path)
    # The blank local endpoint is dropped; the healthy remote one survives.
    assert cfg.local_deployment == []
    assert [d.endpoint for d in cfg.remote_deployment] == ["https://zai.example"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("orphan-model" in m for m in msgs)


@pytest.mark.unit
def test_whitespace_endpoint_is_dropped(tmp_path, caplog):
    """A whitespace-only endpoint is treated as unset and pruned."""
    yaml_path = tmp_path / "routing.yaml"
    yaml_path.write_text('local_deployment:\n  - endpoint: "   "\n    models:\n      - m1\n')
    with caplog.at_level(logging.WARNING, logger="routing.config"):
        cfg = load_routing_config(yaml_path)
    assert cfg.local_deployment == []
