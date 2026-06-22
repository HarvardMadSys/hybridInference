"""Unit tests for KeyPool.add_key / remove_key dynamic mutation."""

from __future__ import annotations

import threading

import pytest

from serving.adapters.key_pool import KeyPool

pytestmark = pytest.mark.unit


def test_add_key_appends_new_slot():
    """Adding an unknown key returns a fresh trailing index and grows the pool."""
    pool = KeyPool(keys=["k0"], provider_label="test")
    idx = pool.add_key("k1")
    assert idx == 1
    assert pool.size() == 2
    assert set(pool.snapshot_keys()) == {"k0", "k1"}


def test_add_key_is_idempotent_for_existing_active_key():
    """Re-adding the same key returns the existing slot and does not duplicate."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    first = pool.add_key("k1")
    second = pool.add_key("k1")
    assert first == second == 1
    assert pool.size() == 2


def test_add_key_reactivates_removed_slot():
    """Adding back a previously removed key reuses the original slot index."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    assert pool.remove_key("k1") is True
    assert pool.size() == 1
    idx = pool.add_key("k1")
    assert idx == 1
    assert pool.size() == 2


def test_remove_key_returns_false_for_unknown_key():
    """Removing a key that was never added is a no-op returning False."""
    pool = KeyPool(keys=["k0"], provider_label="test")
    assert pool.remove_key("missing") is False
    assert pool.size() == 1


def test_remove_key_drops_affinity_entries_pointing_at_removed_slot(monkeypatch):
    """Removed-slot affinity entries must be purged so future acquires re-pick."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Pin user-A to k0, then mute k0 so the next user is forced onto k1.
    key_a, lease_a = pool.acquire("user-A")
    assert key_a == "k0"
    pool.release(lease_a, status_code=429)  # mute k0
    key_b, _ = pool.acquire("user-B")
    assert key_b == "k1"
    assert pool.affinity_count() == 2

    assert pool.remove_key("k1") is True
    # Only user-A's affinity should remain.
    assert pool.affinity_count() == 1

    # Once k0's mute elapses, user-B's next acquire succeeds against it.
    fake_now[0] += 301
    new_key, _ = pool.acquire("user-B")
    assert new_key == "k0"


def test_remove_key_excludes_slot_from_selection():
    """Picker must skip removed slots even when they sort first."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    pool.acquire("user-A")  # picks k0 (first available)
    assert pool.remove_key("k0") is True
    key, _ = pool.acquire("user-B")
    assert key == "k1"


def test_add_remove_are_lock_protected_under_concurrency():
    """Concurrent add/remove from many threads leaves the pool consistent."""
    pool = KeyPool(keys=["base"], provider_label="test")
    iterations = 200

    def worker(tag: str) -> None:
        for i in range(iterations):
            key = f"{tag}-{i}"
            pool.add_key(key)
            pool.remove_key(key)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Only the original "base" key should remain active. Tombstones may be
    # present but size() filters them out.
    assert pool.size() == 1
    assert pool.snapshot_keys() == ["base"]


def test_add_key_made_available_for_acquire(monkeypatch):
    """Newly added keys participate in subsequent acquires immediately."""
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429)  # mute the only existing key
    pool.add_key("k1")
    # With k0 muted, the freshly added key carries the next user.
    key, _ = pool.acquire("user-B")
    assert key == "k1"
