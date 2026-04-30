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


def test_release_with_retry_after_seconds_sets_cooldown(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="30")

    # k0 should be cooled until t=1030
    assert pool._keys[lease.key_index].cooldown_until == pytest.approx(1030.0)


def test_release_with_429_no_retry_after_uses_default_cooldown(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after=None)

    assert pool._keys[0].cooldown_until == pytest.approx(1120.0)  # +120s default


def test_release_with_2xx_does_not_set_cooldown():
    pool = KeyPool(keys=["k0"], provider_label="test")
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=200, retry_after=None)
    assert pool._keys[0].cooldown_until == 0.0


def test_release_with_other_4xx_does_not_set_cooldown():
    pool = KeyPool(keys=["k0"], provider_label="test")
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=401, retry_after=None)
    assert pool._keys[0].cooldown_until == 0.0


def test_retry_after_is_capped_at_one_hour(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="86400")  # 1 day

    assert pool._keys[0].cooldown_until == pytest.approx(1000.0 + 3600.0)


def test_retry_after_negative_falls_back_to_default(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="-5")
    assert pool._keys[0].cooldown_until == pytest.approx(1120.0)


def test_retry_after_http_date_format(monkeypatch):
    """RFC 7231 allows HTTP-date format; we honor it."""
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta

    pool = KeyPool(keys=["k0"], provider_label="test")
    base_real = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Fake time module sees t=1000.0; HTTP-date is base_real + 30s
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Use a known wall-clock anchor by monkeypatching _parse_retry_after's anchor.
    # Easier: pass the date as 30s in the future relative to whatever time.time
    # returns; we patch time.time too.
    fake_wall = [base_real.timestamp()]
    monkeypatch.setattr("serving.adapters.key_pool.time.time", lambda: fake_wall[0])

    future_http_date = format_datetime(base_real + timedelta(seconds=30))

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after=future_http_date)
    # Expect cooldown ≈ now + 30s (capped before 3600)
    assert 1020 <= pool._keys[0].cooldown_until <= 1040


def test_cooldown_key_is_skipped_during_selection(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Burn k0 with a 429
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="30")

    # New user must land on k1 since k0 is in cooldown
    k, _ = pool.acquire("user-B")
    assert k == "k1"


def test_mid_affinity_user_re_picks_when_bound_key_cooled(monkeypatch):
    """If the bound key is cooled mid-window, the user is reassigned."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k_first, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="60")
    # User-A's affinity points at k0, but k0 is cooled
    k_second, _ = pool.acquire("user-A")
    assert k_first == "k0"
    assert k_second == "k1"


def test_all_keys_exhausted_raises_keypoolexhausted(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease0 = pool.acquire("user-A")
    pool.release(lease0, status_code=429, retry_after="30")
    _, lease1 = pool.acquire("user-B")
    pool.release(lease1, status_code=429, retry_after="30")

    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-C")


def test_cooldown_recovers_after_time_passes(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="30")
    # Still in cooldown
    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-B")

    fake_now[0] += 31
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


def test_concurrent_acquires_distribute_evenly():
    """Many threads acquiring as new users spread across keys without race."""
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

    # Total acquires == users
    assert len(results) == NUM_USERS
    # request_count totals across keys equals NUM_USERS (no double-counting,
    # no lost increments)
    assert sum(s.request_count for s in pool._keys) == NUM_USERS

    # Distribution is reasonably balanced — each key gets within +/-20% of mean
    expected = NUM_USERS / NUM_KEYS
    for s in pool._keys:
        assert 0.8 * expected <= s.request_count <= 1.2 * expected, (
            f"unbalanced: {[k.request_count for k in pool._keys]}"
        )


@pytest.mark.parametrize("bad_value", ["abc", "tomorrow", "", "   ", "nan", "inf nope", "NaN"])
def test_release_malformed_retry_after_falls_back_to_default(monkeypatch, bad_value):
    """Per spec: malformed/non-finite Retry-After → 2-min default cooldown."""
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after=bad_value)
    assert pool._keys[0].cooldown_until == pytest.approx(1120.0)
