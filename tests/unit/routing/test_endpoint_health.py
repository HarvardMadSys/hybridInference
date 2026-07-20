"""Unit contracts for the composed endpoint health registry."""

import asyncio
from unittest.mock import AsyncMock, patch

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.routers import FixedRouter
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter


async def test_registry_circuit_lifecycle(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "openai:api.example.com:443"
    registry = EndpointHealthRegistry()

    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        registry.record_failure(endpoint_id, reason="upstream_502")
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED

        registry.record_failure(endpoint_id, reason="upstream_502")
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN

        assert registry.allow_request(endpoint_id) is True
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.HALF_OPEN

        registry.record_success(endpoint_id)
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED
        await asyncio.sleep(0)


def test_snapshot_is_independent_from_registry_state():
    endpoint_id = "openai:api.example.com:443"
    registry = EndpointHealthRegistry()
    registry.record_success(endpoint_id)
    expected = registry.snapshot()

    snapshot = registry.snapshot()
    snapshot[endpoint_id]["availability"] = 0.0
    snapshot[endpoint_id]["circuit_state"] = _CircuitState.OPEN
    snapshot["injected"] = {
        "availability": 0.0,
        "circuit_state": _CircuitState.OPEN,
    }

    assert registry.snapshot() == expected


def test_allow_request_keeps_health_registration_lazy_until_ensure():
    endpoint_id = "openai:api.example.com:443"
    registry = EndpointHealthRegistry()

    assert registry.allow_request(endpoint_id) is True
    assert registry.snapshot() == {}

    registry.ensure(endpoint_id)

    assert registry.snapshot() == {
        endpoint_id: {
            "availability": 1.0,
            "circuit_state": _CircuitState.CLOSED,
        }
    }


def test_router_registry_defaults_are_isolated_and_explicit_injection_is_honored():
    fixed = FixedRouter()
    routewise = RouteWiseRouter(config=RouteWiseConfig())

    fixed._on_success("openai:api.example.com:443")

    assert fixed._health_registry is not routewise._health_registry
    assert routewise.get_provider_status() == {}

    injected = EndpointHealthRegistry()
    assert FixedRouter(health_registry=injected)._health_registry is injected
    assert (
        RouteWiseRouter(config=RouteWiseConfig(), health_registry=injected)._health_registry
        is injected
    )


async def test_registry_instances_are_isolated(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "openai:api.example.com:443"
    first = EndpointHealthRegistry()
    second = EndpointHealthRegistry()

    second.record_success(endpoint_id)
    with patch("routing.endpoint_health.alert_slack", new=AsyncMock()):
        first.record_failure(endpoint_id, reason="upstream_502")
        await asyncio.sleep(0)

    assert first.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    assert first.allow_request(endpoint_id) is False
    assert second.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED
    assert second.allow_request(endpoint_id) is True
