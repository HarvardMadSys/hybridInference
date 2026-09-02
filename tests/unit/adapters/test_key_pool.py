"""Unit tests for KeyPool — sequential rotation with affinity, then a short mute.

Selection is sequential: the pool hands out the earliest usable key and only
advances to a later key once an earlier one is muted or already tried by this
request. *Any* upstream error (429, other 4xx/5xx, or a non-HTTP failure
signalled by ``status_code=0``) moves the request off the leased key.

Rotation comes first: while the caller still has an untried usable key,
``release`` reports ``ROTATED`` and leaves the failing key selectable. Only
when there is nothing left to rotate to does the key take a ``MUTE_SECONDS``
(20 second) cooldown — except that a key-specific failure (429/401/402/403)
landing on the *sole* usable key gets ``SOLE_KEY_BACKOFF_THRESHOLD`` free
passes first, then backs off from ``SOLE_KEY_BACKOFF_BASE_SECONDS`` up to the
same ``MUTE_SECONDS`` ceiling.

``release`` takes the keys this request has already used as ``tried``. Empty
(the default) means no rotation loop is driving it — a rotation loop always
records the key it is about to use — so those releases go straight to the mute
path. Tests that exercise rotation pass ``tried`` explicitly, exactly as the
adapter's loop does.
"""

from __future__ import annotations

import pytest

from serving.adapters.key_pool import (
    KeyPool,
    KeyPoolExhausted,
    KeyPoolRoleRestricted,
    ReleaseOutcome,
    should_mute_status,
)

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
def test_release_mutes_for_mute_seconds_when_nothing_to_rotate_to(monkeypatch, status_code):
    """Key-specific / transient failures (and the network sentinel 0) mute for 20s.

    No ``tried`` set, so this release has no rotation loop behind it (the
    mid-stream case) and takes the mute path straight away.
    """
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
    outcome = pool.release(lease, status_code=status_code)
    assert outcome is ReleaseOutcome.PROPAGATE
    assert pool._keys[0].cooldown_until == 0.0


@pytest.mark.parametrize("status_code", [429, 401, 402, 403])
def test_release_key_specific_error_gives_sole_key_free_passes(monkeypatch, status_code):
    """Quota/auth/payment failures get free passes on the sole key before muting.

    There's nowhere to rotate to, so a single blip must not cost a mute at all
    — only a sustained streak past SOLE_KEY_BACKOFF_THRESHOLD starts muting
    (see test_release_key_specific_error_backs_off_sole_key).
    """
    pool = KeyPool(keys=["only"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    for _ in range(pool.SOLE_KEY_BACKOFF_THRESHOLD):
        assert pool.release(lease, status_code=status_code) is ReleaseOutcome.PROPAGATE
        assert pool._keys[0].cooldown_until == 0.0
        _, lease = pool.acquire("user-A")


def test_release_key_specific_error_backs_off_sole_key_exponentially(monkeypatch):
    """Past the free-pass threshold, sole-key mutes grow to the MUTE_SECONDS cap."""
    pool = KeyPool(keys=["only"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Burn through the free passes first.
    for _ in range(pool.SOLE_KEY_BACKOFF_THRESHOLD):
        _, lease = pool.acquire("user-A")
        assert pool.release(lease, status_code=429) is ReleaseOutcome.PROPAGATE
        fake_now[0] += 1

    expected_durations = [pool.SOLE_KEY_BACKOFF_BASE_SECONDS * (2**step) for step in range(5)]
    for expected in expected_durations:
        _, lease = pool.acquire("user-A")
        before = fake_now[0]
        assert pool.release(lease, status_code=429) is ReleaseOutcome.MUTED
        assert pool._keys[0].cooldown_until == pytest.approx(before + min(expected, MUTE))
        # Jump past this cooldown so the next iteration can acquire again.
        fake_now[0] = pool._keys[0].cooldown_until + 1

    # Enough consecutive failures have now accrued that the mute is pinned at
    # the full MUTE_SECONDS ceiling, same as the nothing-to-rotate-to case.
    _, lease = pool.acquire("user-A")
    before = fake_now[0]
    assert pool.release(lease, status_code=429) is ReleaseOutcome.MUTED
    assert pool._keys[0].cooldown_until == pytest.approx(before + MUTE)


def test_success_resets_sole_key_backoff_streak(monkeypatch):
    """A 2xx clears the consecutive-failure streak, restoring the free passes."""
    pool = KeyPool(keys=["only"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429)  # 1st free pass, consecutive_failures=1
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=200)  # success — resets the streak
    assert pool._keys[0].consecutive_failures == 0

    _, lease = pool.acquire("user-A")
    outcome = pool.release(lease, status_code=429)  # back to the 1st free pass
    assert outcome is ReleaseOutcome.PROPAGATE
    assert pool._keys[0].cooldown_until == 0.0


def test_release_transient_error_keeps_last_usable_key(monkeypatch):
    """In a multi-key pool, a transient error never mutes the last usable key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Mute k0 (key-specific), then a transient 500 hits k1 — the last usable key.
    _, lease0 = pool.acquire("user-A")
    assert pool.release(lease0, status_code=429) is ReleaseOutcome.MUTED
    _, lease1 = pool.acquire("user-B")
    assert lease1.key_index == 1
    outcome = pool.release(lease1, status_code=500)
    assert outcome is ReleaseOutcome.PROPAGATE
    assert pool._keys[1].cooldown_until == 0.0


def test_release_propagates_for_non_muting_statuses():
    """2xx and request-scoped 4xx report PROPAGATE so callers surface the error.

    Passing ``tried`` makes no difference: a request-scoped error fails the same
    way on every key, so it must not rotate even when another key is free.
    """
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    _, lease = pool.acquire("user-A")
    assert pool.release(lease, status_code=200) is ReleaseOutcome.PROPAGATE
    assert pool.release(lease, status_code=400) is ReleaseOutcome.PROPAGATE
    assert pool.release(lease, status_code=400, tried={0}) is ReleaseOutcome.PROPAGATE


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
    """Once k0 is muted, k1 becomes the sole usable key and gets free passes
    before it too mutes — only after burning through those does the pool
    exhaust."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # k0 fails while k1 is still available — mutes immediately (multi-key path).
    _, lease0 = pool.acquire("user-A")
    pool.release(lease0, status_code=429)

    # k1 is now the sole usable key: burn its free passes, then mute it.
    lease1 = None
    for _ in range(pool.SOLE_KEY_BACKOFF_THRESHOLD):
        _, lease1 = pool.acquire("user-B")
        assert pool.release(lease1, status_code=429) is ReleaseOutcome.PROPAGATE
    _, lease1 = pool.acquire("user-B")
    assert pool.release(lease1, status_code=429) is ReleaseOutcome.MUTED

    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-C")


def test_mute_recovers_after_backoff_window(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Burn through the free passes, then trigger the first backoff mute.
    for _ in range(pool.SOLE_KEY_BACKOFF_THRESHOLD):
        _, lease = pool.acquire("user-A")
        pool.release(lease, status_code=429)
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429)
    duration = pool._keys[0].cooldown_until - fake_now[0]
    assert duration == pytest.approx(pool.SOLE_KEY_BACKOFF_BASE_SECONDS)

    # Still muted just before the window closes.
    fake_now[0] += duration - 1
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


# --- rotate first, mute as a last resort -----------------------------------


def test_mute_seconds_is_twenty():
    """The mute window is 20s: long enough to move traffic, short to recover.

    Pinned because it is the number an operator feels — a muted key is capacity
    the pool is not spending, and every caller-facing ``retry-after`` for an
    exhausted pool is derived from it.
    """
    assert KeyPool.MUTE_SECONDS == 20.0


@pytest.mark.parametrize("status_code", [429, 401, 402, 403, 408, 425, 500, 503, 599, 0])
def test_release_rotates_without_muting_while_a_key_is_untried(monkeypatch, status_code):
    """A failure with somewhere to go rotates; the failing key keeps its place."""
    pool = KeyPool(keys=["k0", "k1", "k2"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    outcome = pool.release(lease, status_code=status_code, tried={lease.key_index})

    assert outcome is ReleaseOutcome.ROTATED
    assert pool._keys[lease.key_index].cooldown_until == 0.0
    # The streak still counts, so a key that keeps failing arrives at the mute
    # path already partway through its backoff.
    assert pool._keys[lease.key_index].consecutive_failures == 1


def test_release_mutes_once_every_key_has_been_tried(monkeypatch):
    """The last key a request reaches has nowhere to rotate to, so it mutes."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, first = pool.acquire("user-A")
    assert pool.release(first, status_code=429, tried={0}) is ReleaseOutcome.ROTATED

    _, second = pool.acquire("user-A", exclude={0})
    assert second.key_index == 1
    assert pool.release(second, status_code=429, tried={0, 1}) is ReleaseOutcome.MUTED

    assert pool._keys[0].cooldown_until == 0.0
    assert pool._keys[1].cooldown_until == pytest.approx(1000.0 + MUTE)


def test_acquire_skips_tried_keys(monkeypatch):
    """``exclude`` advances a rotation loop past keys that are usable but spent."""
    pool = KeyPool(keys=["k0", "k1", "k2"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    assert pool.acquire("user-A", exclude=set())[0] == "k0"
    assert pool.acquire("user-A", exclude={0})[0] == "k1"
    assert pool.acquire("user-A", exclude={0, 1})[0] == "k2"

    # Exclusion is per call and never persisted: another caller still starts at
    # the earliest key, because nothing about k0 was muted.
    assert pool.acquire("user-B")[0] == "k0"


def test_acquire_raises_when_every_usable_key_is_excluded():
    """A request that has burned every key it may use gets KeyPoolExhausted.

    Plain ``KeyPoolExhausted``, not ``KeyPoolRoleRestricted``: the caller ran out
    because the endpoint failed it on every key, and excusing that from endpoint
    health would hide a real outage.
    """
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    with pytest.raises(KeyPoolExhausted) as excinfo:
        pool.acquire("user-A", exclude={0, 1})
    assert not isinstance(excinfo.value, KeyPoolRoleRestricted)
    assert "already tried by this request" in str(excinfo.value)


def test_exclusion_exhaustion_is_not_excused_as_a_tier_restriction():
    """Burning your own keys is an endpoint failure, even beside a reserved key.

    The tier question is judged as if this request had not excluded anything:
    a free caller that tried its only shared key and watched it fail has hit the
    endpoint, and a healthy pro-reserved key it was never entitled to must not
    turn that into the health-exempt ``KeyPoolRoleRestricted``.
    """
    pool = KeyPool(
        keys=["shared", "reserved"],
        provider_label="test",
        min_roles={"reserved": "pro"},
    )

    with pytest.raises(KeyPoolExhausted) as excinfo:
        pool.acquire("user-A", role="free", exclude={0})
    assert not isinstance(excinfo.value, KeyPoolRoleRestricted)

    # Without the exclusion the same caller *is* merely tier-restricted, which is
    # what the exempt class is for — the distinction the exclusion must not blur.
    pool._keys[0].cooldown_until = float("inf")
    with pytest.raises(KeyPoolRoleRestricted):
        pool.acquire("user-B", role="free")


def test_rotation_repoints_affinity_onto_the_key_that_served(monkeypatch):
    """Rotating rebinds the caller, so its next request skips the failed key.

    The failing key is left selectable on purpose, so without this a repeat
    caller would pay a wasted upstream round trip on every request.
    """
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    assert pool.release(lease, status_code=429, tried={0}) is ReleaseOutcome.ROTATED
    assert pool.acquire("user-A", exclude={0})[0] == "k1"

    # Next request from the same caller: no exclusions, and it still lands on k1.
    assert pool.acquire("user-A")[0] == "k1"


def test_release_without_tried_takes_the_mute_path(monkeypatch):
    """No ``tried`` means no rotation loop — a mid-stream failure, so mute.

    The empty default cannot mean "nothing tried yet": a rotation loop records
    the key before it can fail, so only a caller that has no way to rotate ever
    releases with an empty set.
    """
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    assert pool.release(lease, status_code=0) is ReleaseOutcome.MUTED
    assert pool._keys[0].cooldown_until == pytest.approx(1000.0 + MUTE)


def test_total_outage_converges_on_a_fully_muted_pool(monkeypatch):
    """Every key dead: each request mutes the one it runs out of alternatives on.

    Rotation-first slows the walk to exhaustion but must not prevent it — a pool
    that never exhausts would never raise ``KeyPoolExhausted``, and the endpoint
    breaker would never see the endpoint fail as a whole.
    """
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    def one_request() -> None:
        """Walk the keys the way the adapter's rotation loop does."""
        tried: set[int] = set()
        for _ in range(pool.size()):
            try:
                _, lease = pool.acquire("user-A", exclude=tried)
            except KeyPoolExhausted:
                return
            tried.add(lease.key_index)
            if pool.release(lease, status_code=429, tried=tried) is ReleaseOutcome.PROPAGATE:
                return

    # k1 mutes on the first request (k0 was rotated past), then k0 spends its
    # sole-key free passes before muting too.
    for _ in range(pool.SOLE_KEY_BACKOFF_THRESHOLD + 1):
        one_request()

    assert pool._keys[0].cooldown_until > fake_now[0]
    assert pool._keys[1].cooldown_until > fake_now[0]
    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-B")
