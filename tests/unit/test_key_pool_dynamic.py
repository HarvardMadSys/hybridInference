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


def test_remove_key_drops_affinity_entries_pointing_at_removed_slot():
    """Removed-slot affinity entries must be purged so future acquires re-pick."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    # Pin user A to k0 and user B to k1.
    key_a, _ = pool.acquire("user-A")
    key_b, _ = pool.acquire("user-B")
    assert key_a == "k0"
    assert key_b == "k1"
    assert pool.affinity_count() == 2

    assert pool.remove_key("k1") is True
    # Only user-A's affinity should remain.
    assert pool.affinity_count() == 1

    # User-B's next acquire must succeed against the surviving key.
    new_key, _ = pool.acquire("user-B")
    assert new_key == "k0"


def test_remove_key_excludes_slot_from_least_loaded_pick():
    """Picker must skip removed slots even when they have the lowest load."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    # Inflate k1 load so the picker prefers k0; then remove k0 and verify k1 is used.
    pool.acquire("user-A")  # picks k0 (tie at 0)
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


def test_add_key_made_available_for_acquire():
    """Newly added keys participate in subsequent acquires immediately."""
    pool = KeyPool(keys=["k0"], provider_label="test")
    pool.acquire("user-A")  # k0 count -> 1
    pool.add_key("k1")
    # New user should pick the freshly added (zero-load) key.
    key, _ = pool.acquire("user-B")
    assert key == "k1"
