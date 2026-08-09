"""Unit tests for KeyPool tier reservation (per-key ``min_role``).

A key's ``min_role`` is the lowest role allowed to spend it. ``"free"`` (the
default) reserves nothing. A caller below a key's ``min_role`` never sees that
key — not through selection, not through an existing affinity binding, and not
as a fallback that would cancel its sole-key protection. Among the keys a caller
*may* use, the most-reserved ones go first so entitled traffic drains the
capacity set aside for it before touching the shared pool.
"""

from __future__ import annotations

import pytest

from serving.adapters.key_pool import KeyPool, KeyPoolExhausted, normalize_min_role


def _pool(**min_roles: str) -> KeyPool:
    """Build a pool over the given ``key=min_role`` pairs, in argument order."""
    return KeyPool(keys=list(min_roles), provider_label="test", min_roles=dict(min_roles))


# --- normalization ---------------------------------------------------------


@pytest.mark.parametrize("value", ["free", "pro", "internal", "admin"])
def test_normalize_accepts_every_known_role(value):
    assert normalize_min_role(value) == value


@pytest.mark.parametrize("value", [None, "", "trial", "PRO", "superuser", 3])
def test_normalize_falls_back_to_free_for_unusable_values(value):
    """An uninterpretable reservation must not take the key out of service."""
    assert normalize_min_role(value) == "free"


# --- selection -------------------------------------------------------------


def test_free_caller_never_selects_a_reserved_key():
    pool = _pool(shared="free", reserved="pro")
    key, lease = pool.acquire("user-A", role="free")
    assert key == "shared"
    assert lease.role == "free"


def test_entitled_caller_prefers_the_reserved_key_over_the_shared_one():
    """Reservation reorders selection: pro drains pro capacity first."""
    pool = _pool(shared="free", reserved="pro")
    key, _ = pool.acquire("user-A", role="pro")
    assert key == "reserved"


def test_higher_tier_wins_over_lower_reservation():
    pool = _pool(shared="free", pro_key="pro", internal_key="internal")
    assert pool.acquire("u", role="internal")[0] == "internal_key"
    assert pool.acquire("v", role="pro")[0] == "pro_key"
    assert pool.acquire("w", role="free")[0] == "shared"


def test_config_order_breaks_ties_within_a_tier():
    pool = KeyPool(
        keys=["a", "b"],
        provider_label="test",
        min_roles={"a": "pro", "b": "pro"},
    )
    assert pool.acquire("u", role="pro")[0] == "a"


def test_role_none_is_an_unrestricted_internal_caller():
    """Health probes and warmups carry no user identity and see every key."""
    pool = _pool(reserved="admin")
    assert pool.acquire("probe", role=None)[0] == "reserved"


def test_free_caller_exhausts_when_only_reserved_keys_remain():
    pool = _pool(reserved="pro")
    with pytest.raises(KeyPoolExhausted) as excinfo:
        pool.acquire("user-A", role="free")
    assert "reserved for a higher tier" in str(excinfo.value)


def test_free_caller_falls_back_past_a_muted_shared_key_only_to_shared_keys(monkeypatch):
    pool = _pool(shared1="free", reserved="pro", shared2="free")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A", role="free")
    assert pool.release(lease, status_code=429) is True  # shared1 muted

    assert pool.acquire("user-B", role="free")[0] == "shared2"


# --- size ------------------------------------------------------------------


def test_size_counts_only_the_keys_the_role_may_use():
    pool = _pool(shared="free", reserved="pro", locked="admin")
    assert pool.size("free") == 1
    assert pool.size("pro") == 2
    assert pool.size("admin") == 3
    assert pool.size() == 3  # unrestricted


# --- affinity --------------------------------------------------------------


def test_affinity_is_dropped_when_the_bound_key_is_re_tiered(monkeypatch):
    """A live binding must not outlive the caller's entitlement to that key."""
    pool = _pool(k0="free", k1="free")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    assert pool.acquire("user-A", role="free")[0] == "k0"
    assert pool.set_key_min_role("k0", "pro") is True
    assert pool.acquire("user-A", role="free")[0] == "k1"
    # ...while a caller that does qualify goes right back to it.
    assert pool.acquire("user-B", role="pro")[0] == "k0"


def test_set_key_min_role_reports_missing_keys():
    pool = _pool(k0="free")
    assert pool.set_key_min_role("nope", "pro") is False


def test_add_key_carries_a_reservation_and_re_add_preserves_it():
    pool = _pool(k0="free")
    pool.add_key("k1", min_role="pro")
    assert pool.snapshot_min_roles() == {"k0": "free", "k1": "pro"}

    pool.add_key("k1")  # re-add with no explicit tier
    assert pool.snapshot_min_roles()["k1"] == "pro"

    pool.add_key("k1", min_role="free")  # explicit demotion
    assert pool.snapshot_min_roles()["k1"] == "free"


# --- release / sole-key protection -----------------------------------------


def test_reserved_key_does_not_count_as_a_fallback_for_a_free_caller(monkeypatch):
    """The sole-key backoff is judged from the leaseholder's own key set.

    A free caller cannot rotate onto the pro key, so its one shared key is a
    *sole* key and keeps its free passes — muting it immediately would blank the
    whole free tier on a single 429.
    """
    pool = _pool(shared="free", reserved="pro")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A", role="free")
    assert pool.release(lease, status_code=429) is False

    # The same failure from a pro caller *does* mute immediately: it has the
    # shared key to rotate to.
    _, pro_lease = pool.acquire("user-B", role="pro")
    assert pro_lease.key_index == 1
    assert pool.release(pro_lease, status_code=429) is True


def test_muting_a_reserved_key_leaves_the_shared_key_serving(monkeypatch):
    pool = _pool(shared="free", reserved="pro")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A", role="pro")
    assert pool.release(lease, status_code=429) is True

    assert pool.acquire("user-A", role="pro")[0] == "shared"
    assert pool.acquire("user-B", role="free")[0] == "shared"
