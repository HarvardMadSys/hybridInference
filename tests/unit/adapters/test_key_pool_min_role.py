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

from serving.adapters.key_pool import (
    KeyPool,
    KeyPoolExhausted,
    KeyPoolRoleRestricted,
    normalize_min_role,
)


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


# --- role restriction vs a dead pool ---------------------------------------


def test_role_restriction_raises_the_health_neutral_subclass():
    """A tier-only shutout is not an endpoint failure — it has its own type."""
    pool = _pool(reserved="pro")
    with pytest.raises(KeyPoolRoleRestricted):
        pool.acquire("user-A", role="free")


def test_a_pool_usable_by_nobody_raises_plain_exhausted(monkeypatch):
    """When no key can serve anyone, the endpoint really is in trouble."""
    pool = _pool(shared="free")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A", role="pro")
    for _ in range(pool.SOLE_KEY_BACKOFF_THRESHOLD + 1):
        _, lease = pool.acquire("user-A", role="pro")
        pool.release(lease, status_code=429)

    with pytest.raises(KeyPoolExhausted) as excinfo:
        pool.acquire("user-B", role="free")
    assert not isinstance(excinfo.value, KeyPoolRoleRestricted)


def test_shared_key_muted_but_reserved_healthy_is_role_restricted(monkeypatch):
    """The distinction is "can anyone be served", not "is any key reserved".

    A free caller whose only shared key is cooling down while a reserved key is
    healthy must not count against the endpoint: it is still serving pro traffic.
    """
    pool = _pool(shared="free", reserved="pro")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A", role="pro")  # takes 'reserved'
    assert lease.key_index == 1
    _, shared_lease = pool.acquire("user-B", role="free")
    assert pool.release(shared_lease, status_code=429) is False  # sole key for free
    # Force the shared key into cooldown from an unrestricted caller's lease,
    # which does have somewhere to rotate to.
    _, any_lease = pool.acquire("probe", role=None)
    pool._keys[0].cooldown_until = fake_now[0] + 60.0

    with pytest.raises(KeyPoolRoleRestricted):
        pool.acquire("user-C", role="free")
    assert any_lease is not None


def test_only_dynamic_keys_writes_a_pool_entry_tier():
    """``dynamic_keys`` is the sole writer of a pool entry's tier.

    Three review findings came from the tier being written at several call sites:
    whichever ran last won, so a rebuild or an unrelated key add silently dropped a
    reservation. The invariant that replaced them is structural, so guard it
    structurally — a new call site is exactly the regression this catches.
    """
    from pathlib import Path

    backend = Path(__file__).resolve().parents[3] / "apps" / "backend"
    allowed = {
        backend / "serving" / "adapters" / "key_pool.py",  # defines it
        backend / "serving" / "adapters" / "dynamic_keys.py",  # the one authority
    }

    offenders: list[str] = []
    for path in backend.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in (".set_key_min_role(", "min_roles="):
            if marker in text:
                offenders.append(f"{path.relative_to(backend)} uses {marker}")

    assert not offenders, (
        "tier writes must go through dynamic_keys' resolver + sweep, not a direct "
        f"pool write: {offenders}"
    )


# --- one slot per credential ------------------------------------------------


def test_constructor_dedupes_a_repeated_key():
    """A credential listed twice must not become two slots with two tiers.

    Nothing upstream guarantees uniqueness — a route's ``api_keys`` can name two
    env vars holding the same value — and two slots means re-tiering updates one
    while selection can still hand out the other.
    """
    pool = KeyPool(keys=["dup", "other", "dup"], provider_label="test")

    assert pool.snapshot_keys() == ["dup", "other"]
    assert pool.size() == 2


def test_re_tiering_a_repeated_key_leaves_no_untiered_slot():
    """Regression: the reserved key must not stay acquirable by a free caller."""
    pool = KeyPool(keys=["dup", "dup"], provider_label="test")
    assert pool.set_key_min_role("dup", "pro") is True

    assert pool.snapshot_min_roles() == {"dup": "pro"}
    with pytest.raises(KeyPoolRoleRestricted):
        pool.acquire("free-user", role="free")
    assert pool.acquire("pro-user", role="pro")[0] == "dup"


def test_re_tiering_updates_every_live_slot_for_a_key(monkeypatch):
    """Defensive: a remove/re-add cycle can leave a tombstone beside a live slot."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Tombstone k0's slot, then re-add the same value — ``add_key`` reactivates the
    # existing slot, so this asserts the invariant rather than creating a duplicate.
    assert pool.remove_key("k0") is True
    pool.add_key("k0")
    assert pool.set_key_min_role("k0", "internal") is True

    live = [s for s in pool._keys if not s.removed and s.key == "k0"]
    assert live and all(s.min_role == "internal" for s in live)


# --- "can serve now" vs "counts toward rotation" -----------------------------


def test_can_serve_role_is_false_once_the_callers_only_key_is_muted(monkeypatch):
    """``size`` counts muted keys on purpose; ``can_serve_role`` must not.

    A caller committed to one pool with no rotation loop needs the question
    ``acquire`` answers, or it preselects a pool that immediately fails.
    """
    pool = _pool(shared="free", reserved="pro")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    pool._keys[0].cooldown_until = fake_now[0] + 60.0  # shared muted

    assert pool.size("free") == 1  # still counted for rotation bounds
    assert pool.can_serve_role("free") is False
    # The reserved key is healthy, so the pool still serves pro and unrestricted.
    assert pool.can_serve_role("pro") is True
    assert pool.can_serve_role() is True


def test_can_serve_role_recovers_when_the_mute_expires(monkeypatch):
    pool = _pool(shared="free")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    pool._keys[0].cooldown_until = fake_now[0] + 60.0
    assert pool.can_serve_role("free") is False

    fake_now[0] += 61.0
    assert pool.can_serve_role("free") is True


# --- affinity follows a reservation change ----------------------------------


def test_reserving_a_key_pulls_an_affine_entitled_caller_onto_it(monkeypatch):
    """Reservation exists to move entitled traffic — a live binding must not block it.

    Dropping only bindings that point at the re-tiered key left an already-affine
    pro caller draining shared capacity for the rest of the affinity TTL, which is
    the capacity the reservation was meant to protect.
    """
    pool = _pool(shared="free", spare="free")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    assert pool.acquire("pro-user", role="pro")[0] == "shared"
    assert pool.affinity_count() == 1

    # A second key becomes pro-reserved; the pro caller should prefer it at once.
    assert pool.set_key_min_role("spare", "pro") is True
    assert pool.acquire("pro-user", role="pro")[0] == "spare"


def test_free_callers_keep_their_binding_when_another_key_is_reserved(monkeypatch):
    """Only callers whose preferred key moved are re-picked."""
    pool = _pool(shared="free", spare="free")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    assert pool.acquire("free-user", role="free")[0] == "shared"
    pool.set_key_min_role("spare", "pro")

    # 'shared' is still the free tier's preferred key, so the binding survives.
    assert pool.affinity_count() == 1
    assert pool.acquire("free-user", role="free")[0] == "shared"


def test_releasing_a_reservation_returns_entitled_callers_to_order(monkeypatch):
    """Clearing a tier is a declaration change too, so preference re-converges."""
    pool = _pool(first="free", reserved="pro")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    assert pool.acquire("pro-user", role="pro")[0] == "reserved"
    assert pool.set_key_min_role("reserved", "free") is True
    # With nothing reserved, configuration order rules again.
    assert pool.acquire("pro-user", role="pro")[0] == "first"


def test_an_unchanged_re_tier_leaves_affinities_alone(monkeypatch):
    """Re-applying the tier a key already has is a no-op, bindings included."""
    pool = _pool(shared="free", reserved="pro")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    pool.acquire("free-user", role="free")
    before = pool.affinity_count()
    assert pool.set_key_min_role("reserved", "pro") is True
    assert pool.affinity_count() == before
