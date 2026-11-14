"""Key-pool refusals must not open the circuit.

Both ``KeyPoolExhausted`` refusals are raised by ``acquire()`` before anything
is sent upstream, so neither is evidence about the endpoint:

- ``KeyPoolRoleRestricted``: no key this caller's tier may spend, though the
  pool still serves the tiers that own those keys. Counting it would let a
  burst of lower-tier traffic strip capacity reserved from exactly the callers
  it was reserved for — and re-trip on every half-open probe.
- plain ``KeyPoolExhausted``: every key muted (the tail of a provider 429/auth
  storm), removed, or already tried by this request. The mutes themselves keep
  traffic off a struggling provider while they last, and the failures that did
  the muting were counted when they happened. Counting the echo let one muted
  pool open the circuit on every route sharing the provider's keys, ending
  streams in ``AllCircuitsOpenError`` (issue #1440).

``EndpointHealthRegistry.record_failure`` exempts both, with distinct log
labels so operators can tell reserved-capacity refusals from pool-wide outage
tails.
"""

import pytest

from routing.completion_outcome import CompletionOutcome
from routing.endpoint_health import EndpointHealthRegistry, _CircuitState
from serving.adapters.key_pool import KeyPoolExhausted, KeyPoolRoleRestricted


def test_role_restricted_refusal_is_breaker_exempt(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    endpoint_id = "zai:api.example.com:443"
    registry.record_success(endpoint_id, outcome=CompletionOutcome.PROGRESS)
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


def test_pool_exhausted_for_everyone_is_also_breaker_exempt(monkeypatch):
    """The #1238 narrowness was the bug: a muted pool is not an endpoint fault.

    The pool usable by nobody is the tail of upstream failures that were
    already counted (429/401/5xx mutes the keys); the exhaustion echo adds no
    endpoint evidence, and charging it re-trips the breaker on every half-open
    probe while the mutes outlive the cooldown (issue #1440).
    """
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_MIN_AVAILABILITY", "0.0")

    registry = EndpointHealthRegistry()
    endpoint_id = "zai:api.example.com:8443"
    registry.record_success(endpoint_id, outcome=CompletionOutcome.PROGRESS)
    baseline = registry.snapshot()[endpoint_id]["availability"]

    for _ in range(5):
        registry.record_failure(
            endpoint_id,
            reason="chat_exception",
            exc=KeyPoolExhausted("all keys muted"),
        )

    status = registry.snapshot()[endpoint_id]
    assert status["circuit_state"] == _CircuitState.CLOSED
    assert status["availability"] == baseline


def test_subclass_relationship_keeps_existing_handlers_working():
    """Existing ``except KeyPoolExhausted`` sites (the 429 mapping) still catch it."""
    assert issubclass(KeyPoolRoleRestricted, KeyPoolExhausted)
    with pytest.raises(KeyPoolExhausted):
        raise KeyPoolRoleRestricted("no key for role=free")
