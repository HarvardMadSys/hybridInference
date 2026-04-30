"""Unit tests for KeyPool — multi-key rotation with affinity + cooldown."""

from __future__ import annotations

import pytest

from serving.adapters.key_pool import KeyPool, KeyPoolExhausted


def test_keypool_module_exports():
    """KeyPool and KeyPoolExhausted are importable from the new module."""
    assert KeyPool is not None
    assert issubclass(KeyPoolExhausted, Exception)


def test_keypool_constructor_rejects_empty_keys():
    """Constructing with zero keys is a config error."""
    with pytest.raises(ValueError):
        KeyPool(keys=[], provider_label="test")


def test_keypool_constructor_accepts_single_key():
    """Single-key pool is valid (degenerate case)."""
    pool = KeyPool(keys=["k1"], provider_label="test")
    assert pool.size() == 1


def test_acquire_single_key_returns_that_key():
    pool = KeyPool(keys=["only-key"], provider_label="test")
    key, lease = pool.acquire("user-A")
    assert key == "only-key"
    assert lease.key_index == 0
    assert lease.affinity_key == "user-A"


def test_acquire_picks_least_loaded_key_for_new_user():
    """First user picks index 0 (tie at request_count=0); load increments."""
    pool = KeyPool(keys=["k0", "k1", "k2"], provider_label="test")

    # First two new users go to k0 then k1 — counters increment under the lock,
    # and ties break by lowest index.
    k_a, _ = pool.acquire("user-A")
    k_b, _ = pool.acquire("user-B")
    k_c, _ = pool.acquire("user-C")

    assert k_a == "k0"
    assert k_b == "k1"
    assert k_c == "k2"


def test_acquire_increments_request_count_on_each_call():
    """Even affinity-reused acquires bump request_count for the bound key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    pool.acquire("user-A")
    pool.acquire("user-A")
    pool.acquire("user-A")

    # Internal inspection — the bound key has count=3, the other has 0.
    counts = sorted(s.request_count for s in pool._keys)
    assert counts == [0, 3]


def test_same_user_keeps_same_key_within_ttl(monkeypatch):
    """Same affinity_key returns the same index for 5 minutes."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    # Freeze time at t0
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k1, _ = pool.acquire("user-A")
    fake_now[0] += 60  # +60s
    k2, _ = pool.acquire("user-A")
    fake_now[0] += 200  # +200s — still within 300s
    k3, _ = pool.acquire("user-A")

    assert k1 == k2 == k3


def test_affinity_expires_after_ttl(monkeypatch):
    """After 5 minutes, the user may land on a different key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k_first, _ = pool.acquire("user-A")
    # Advance well past 300s
    fake_now[0] += 301
    # Make k0 look heavily loaded so the new pick goes to k1
    pool._keys[0].request_count = 1000

    k_second, _ = pool.acquire("user-A")
    assert k_first == "k0"
    assert k_second == "k1"


def test_different_users_can_share_or_split_keys():
    """Two new users in a 2-key pool end up on different keys (load-spread)."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    a, _ = pool.acquire("user-A")
    b, _ = pool.acquire("user-B")
    assert {a, b} == {"k0", "k1"}
