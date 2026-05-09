"""Unit tests for RoutingConfig migration of legacy fields."""

from __future__ import annotations

import logging

import pytest

from routing.config import RoutingConfig


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
