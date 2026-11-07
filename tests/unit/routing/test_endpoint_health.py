"""Unit contracts for the composed endpoint health registry."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from routing.routers import FixedRouter, RoutingObservation
from routing.routewise.config import RouteWiseConfig
from routing.routewise.router import RouteWiseRouter
from serving.adapters.key_pool import KeyPoolExhausted, KeyPoolRoleRestricted


async def test_registry_circuit_lifecycle(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "openai:api.example.com:443"
    registry = EndpointHealthRegistry()

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(endpoint_id, reason="upstream_502")
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED

        registry.record_failure(endpoint_id, reason="upstream_502")
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN

        # Asking is pure: the cooldown has elapsed, so the endpoint reads as
        # admissible, but nothing moves until a caller commits to dispatching.
        assert registry.allow_request(endpoint_id) is True
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN

        assert registry.begin_dispatch(endpoint_id) is not None
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
            "last_error_status": None,
            "consecutive_auth_rejections": 0,
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


def test_fixed_router_record_observation_is_an_explicit_noop():
    endpoint_id = "openai:api.example.com:443"
    fixed = FixedRouter()
    fixed._on_success(endpoint_id)
    baseline = fixed.get_provider_status()

    result = fixed.record_observation(
        RoutingObservation(
            model_id="model",
            endpoint_id=endpoint_id,
            ttft_ms=1.0,
            total_latency_ms=2.0,
            token_count=3,
            success=False,
        )
    )

    assert result is None
    assert fixed.get_provider_status() == baseline


async def test_registry_instances_are_isolated(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "openai:api.example.com:443"
    first = EndpointHealthRegistry()
    second = EndpointHealthRegistry()

    second.record_success(endpoint_id)
    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        first.record_failure(endpoint_id, reason="upstream_502")
        await asyncio.sleep(0)

    assert first.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    assert first.allow_request(endpoint_id) is False
    assert second.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED
    assert second.allow_request(endpoint_id) is True


class _StatusError(Exception):
    """Adapter exception carrying an upstream HTTP status (aiohttp uses .status)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


async def test_key_pool_exhaustion_does_not_trip_the_breaker(monkeypatch, caplog):
    # Issue #1440: "no usable API key" is raised by KeyPool.acquire before
    # anything is sent, so it is a local precondition refusal, not evidence
    # against the endpoint. Counting it let one muted key pool open the circuit
    # on every route sharing the provider's keys (AllCircuitsOpenError mid-stream).
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "glm-5.3-flash:zai-api"
    registry = EndpointHealthRegistry()
    registry.ensure(endpoint_id)

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(3):
            registry.record_failure(
                endpoint_id,
                reason="stream_exception",
                detail="No usable API key for provider zai (role=pro, 2 configured)",
                exc=KeyPoolExhausted("No usable API key for provider zai"),
            )

    snapshot = registry.snapshot()[endpoint_id]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    assert snapshot["availability"] == 1.0
    assert registry.allow_request(endpoint_id) is True
    assert [r for r in caplog.records if r.getMessage() == "key_pool_exhausted_skip_breaker"]


async def test_key_pool_exhaustion_does_not_mask_a_real_outage(monkeypatch):
    # The exemption must not swallow a genuine upstream fault: real failures
    # (here 429s, which are counted) still trip the breaker even when the
    # exhaustion echo interleaves with them.
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "glm-4.6:zai-api"
    registry = EndpointHealthRegistry()
    registry.ensure(endpoint_id)

    with patch("serving.observability.alerts.alert_slack", new=AsyncMock()):
        registry.record_failure(endpoint_id, reason="upstream_429", exc=_StatusError(429))
        registry.record_failure(
            endpoint_id,
            reason="stream_exception",
            exc=KeyPoolExhausted("No usable API key for provider zai"),
        )
        assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.CLOSED

        registry.record_failure(endpoint_id, reason="upstream_429", exc=_StatusError(429))
        await asyncio.sleep(0)

    assert registry.snapshot()[endpoint_id]["circuit_state"] == _CircuitState.OPEN
    assert registry.allow_request(endpoint_id) is False


async def test_role_restricted_exhaustion_keeps_its_own_skip_event(monkeypatch, caplog):
    # The reserved-above-tier subtype keeps its distinct event (added in #1238);
    # only the log label distinguishes it from plain exhaustion now.
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")
    endpoint_id = "glm-4.6:zai-api"
    registry = EndpointHealthRegistry()
    registry.ensure(endpoint_id)

    with caplog.at_level(logging.INFO, logger="routing.routers"):
        for _ in range(3):
            registry.record_failure(
                endpoint_id,
                reason="stream_exception",
                exc=KeyPoolRoleRestricted("keys reserved for a higher tier"),
            )

    snapshot = registry.snapshot()[endpoint_id]
    assert snapshot["circuit_state"] == _CircuitState.CLOSED
    assert registry.allow_request(endpoint_id) is True
    assert [r for r in caplog.records if r.getMessage() == "role_restricted_skip_breaker"]
    assert not [r for r in caplog.records if r.getMessage() == "key_pool_exhausted_skip_breaker"]
