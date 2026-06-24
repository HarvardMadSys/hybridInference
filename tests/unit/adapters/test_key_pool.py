"""Unit tests for KeyPool — sequential rotation with affinity + 5-min mute.

Selection is sequential: the pool hands out the earliest usable key and only
advances to a later key once an earlier one is muted. *Any* upstream error
(429, other 4xx/5xx, or a non-HTTP failure signalled by ``status_code=0``)
mutes the leased key for ``MUTE_SECONDS`` (5 minutes).
"""

from __future__ import annotations

import pytest

from serving.adapters.key_pool import KeyPool, KeyPoolExhausted, should_mute_status

MUTE = KeyPool.MUTE_SECONDS


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


def test_new_users_all_get_the_first_key():
    """Sequential selection: every new user lands on k0 while it is usable."""
    pool = KeyPool(keys=["k0", "k1", "k2"], provider_label="test")

    k_a, _ = pool.acquire("user-A")
    k_b, _ = pool.acquire("user-B")
    k_c, _ = pool.acquire("user-C")

    assert k_a == k_b == k_c == "k0"


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


def test_affinity_re_pick_after_ttl_stays_on_first_key(monkeypatch):
    """After the affinity TTL, a fresh pick still lands on the first usable key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k_first, _ = pool.acquire("user-A")
    # Advance well past the affinity TTL; k0 was never muted, so it is re-picked.
    fake_now[0] += 301

    k_second, _ = pool.acquire("user-A")
    assert k_first == k_second == "k0"


def test_concurrent_users_share_the_first_key():
    """Two new users in a 2-key pool both land on k0 (sequential)."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    a, _ = pool.acquire("user-A")
    b, _ = pool.acquire("user-B")
    assert a == b == "k0"


@pytest.mark.parametrize("status_code", [429, 401, 402, 403, 408, 425, 500, 503, 599, 0])
def test_release_key_specific_or_transient_error_mutes_for_five_minutes(monkeypatch, status_code):
    """Key-specific / transient failures (and the network sentinel 0) mute for 5 min."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=status_code)

    assert pool._keys[lease.key_index].cooldown_until == pytest.approx(1000.0 + MUTE)


@pytest.mark.parametrize("status_code", [400, 404, 405, 409, 413, 415, 422, 451])
def test_release_request_scoped_4xx_does_not_mute(status_code):
    """Request-scoped client errors fail on every key, so they must not mute."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=status_code)
    assert pool._keys[lease.key_index].cooldown_until == 0.0


@pytest.mark.parametrize("status_code", [200, 201, 204, 299])
def test_release_with_2xx_does_not_mute(status_code):
    pool = KeyPool(keys=["k0"], provider_label="test")
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=status_code)
    assert pool._keys[0].cooldown_until == 0.0


@pytest.mark.parametrize("status_code", [408, 425, 500, 503, 599, 0])
def test_release_transient_error_does_not_mute_sole_key(status_code):
    """A transient/provider-side error must not take the only usable key offline."""
    pool = KeyPool(keys=["only"], provider_label="test")
    _, lease = pool.acquire("user-A")
    muted = pool.release(lease, status_code=status_code)
    assert muted is False
    assert pool._keys[0].cooldown_until == 0.0


@pytest.mark.parametrize("status_code", [429, 401, 402, 403])
def test_release_key_specific_error_mutes_sole_key(monkeypatch, status_code):
    """Quota/auth/payment failures mute even the only key — retrying can't help."""
    pool = KeyPool(keys=["only"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    muted = pool.release(lease, status_code=status_code)
    assert muted is True
    assert pool._keys[0].cooldown_until == pytest.approx(1000.0 + MUTE)


def test_release_transient_error_keeps_last_usable_key(monkeypatch):
    """In a multi-key pool, a transient error never mutes the last usable key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Mute k0 (key-specific), then a transient 500 hits k1 — the last usable key.
    _, lease0 = pool.acquire("user-A")
    assert pool.release(lease0, status_code=429) is True
    _, lease1 = pool.acquire("user-B")
    assert lease1.key_index == 1
    muted = pool.release(lease1, status_code=500)
    assert muted is False
    assert pool._keys[1].cooldown_until == 0.0


def test_release_returns_false_for_non_muting_statuses():
    """2xx and request-scoped 4xx report not-muted so callers propagate."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    _, lease = pool.acquire("user-A")
    assert pool.release(lease, status_code=200) is False
    assert pool.release(lease, status_code=400) is False


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (0, True),
        (200, False),
        (204, False),
        (400, False),
        (401, True),
        (402, True),
        (403, True),
        (404, False),
        (408, True),
        (422, False),
        (425, True),
        (429, True),
        (499, False),
        (500, True),
        (503, True),
        (599, True),
    ],
)
def test_should_mute_status(status_code, expected):
    assert should_mute_status(status_code) is expected


def test_muted_key_is_skipped_during_selection(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Burn k0 with an error.
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429)

    # New user must land on k1 since k0 is muted.
    k, _ = pool.acquire("user-B")
    assert k == "k1"


def test_mid_affinity_user_re_picks_when_bound_key_muted(monkeypatch):
    """If the bound key is muted mid-window, the user is reassigned."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k_first, lease = pool.acquire("user-A")
    pool.release(lease, status_code=500)
    # User-A's affinity points at k0, but k0 is muted.
    k_second, _ = pool.acquire("user-A")
    assert k_first == "k0"
    assert k_second == "k1"


def test_all_keys_muted_raises_keypoolexhausted(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Both keys hit a key-specific failure (429), so even the last one is muted.
    _, lease0 = pool.acquire("user-A")
    pool.release(lease0, status_code=429)
    _, lease1 = pool.acquire("user-B")
    pool.release(lease1, status_code=429)

    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-C")


def test_mute_recovers_after_five_minutes(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429)
    # Still muted just before the window closes.
    fake_now[0] += MUTE - 1
    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-B")

    # Window elapsed — key is usable again.
    fake_now[0] += 2
    k, _ = pool.acquire("user-B")
    assert k == "k0"


def test_affinity_sweep_drops_expired_entries(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Seed affinity dict with 1500 expired-soon entries
    for i in range(1500):
        pool.acquire(f"user-{i}")

    fake_now[0] += 301  # everything expired now
    pool.acquire("trigger-sweep")

    # Sweep happened: expired entries should be gone, only the new one remains
    # plus any whose affinity wasn't expired (none, since we advanced past TTL)
    assert len(pool._affinity) == 1
    assert "trigger-sweep" in pool._affinity


def test_concurrent_acquires_all_use_the_first_key():
    """Many threads acquiring as new users all concentrate on k0, no race."""
    import threading

    NUM_KEYS = 4
    NUM_USERS = 400

    pool = KeyPool(keys=[f"k{i}" for i in range(NUM_KEYS)], provider_label="test")

    results: list[int] = []
    lock = threading.Lock()

    def worker(uid: int) -> None:
        _, lease = pool.acquire(f"user-{uid}")
        with lock:
            results.append(lease.key_index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_USERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every acquire landed on the first key; no increments were lost.
    assert len(results) == NUM_USERS
    assert all(idx == 0 for idx in results)
    assert pool._keys[0].request_count == NUM_USERS
    assert all(s.request_count == 0 for s in pool._keys[1:])
