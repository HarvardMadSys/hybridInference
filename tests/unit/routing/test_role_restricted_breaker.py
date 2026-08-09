"""Tier-reserved keys must not let lower-tier traffic open the circuit.

A caller whose role may spend none of an endpoint's keys is refused by the key
pool *before* anything is sent upstream. That refusal says nothing about the
endpoint: it is still serving the tiers that own those keys. Counting it would
let a burst of free-tier requests trip the breaker and strip reserved capacity
from exactly the callers it was reserved for — and re-trip it on every half-open
probe. So ``KeyPoolRoleRestricted`` is health-neutral, while a plain
``KeyPoolExhausted`` (nothing usable by anyone) still counts.
"""

import pytest

from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from serving.adapters.key_pool import KeyPoolExhausted, KeyPoolRoleRestricted


def test_role_restricted_refusal_is_breaker_exempt(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    endpoint_id = "zai:api.example.com:443"
    registry.record_success(endpoint_id)
    baseline = registry.snapshot()[endpoint_id]["availability"]

    # More consecutive refusals than the threshold: a real failure class would
    # have opened the circuit well before the last one.
    for _ in range(5):
        registry.record_failure(
            endpoint_id,
            reason="chat_exception",
            exc=KeyPoolRoleRestricted("no key for role=free"),
        )

    status = registry.snapshot()[endpoint_id]
    assert status["circuit_state"] == _CircuitState.CLOSED
    assert status["availability"] == baseline


def test_pool_exhausted_for_everyone_still_counts(monkeypatch):
    """The exemption is narrow: a pool usable by nobody is an endpoint fault."""
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    endpoint_id = "zai:api.example.com:8443"
    registry.record_success(endpoint_id)
    baseline = registry.snapshot()[endpoint_id]["availability"]

    for _ in range(2):
        registry.record_failure(
            endpoint_id,
            reason="chat_exception",
            exc=KeyPoolExhausted("all keys muted"),
        )

    status = registry.snapshot()[endpoint_id]
    assert status["circuit_state"] == _CircuitState.OPEN
    assert status["availability"] < baseline


def test_subclass_relationship_keeps_existing_handlers_working():
    """Existing ``except KeyPoolExhausted`` sites (the 429 mapping) still catch it."""
    assert issubclass(KeyPoolRoleRestricted, KeyPoolExhausted)
    with pytest.raises(KeyPoolExhausted):
        raise KeyPoolRoleRestricted("no key for role=free")
