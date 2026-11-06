"""A write about one account evicts that account's cached auth, and nobody else's.

Every auth entry used to be flushed -- an ``fnmatch`` scan of the whole cache
under its lock -- whenever any account saved a UI preference, changed a key or
was approved. Entries are now tagged with their key's account and evicted
through the cache's tag index, so one account's write costs only its own
entries and every other caller keeps its cached auth. A change to every account
at once (a role's quota) still flushes them all.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage.cache import CachedOperationalStore, InMemoryCache

pytestmark = pytest.mark.asyncio

_ROWS = {
    "h1": {"id": 1, "user_id": "u1", "role": "free"},
    "h1b": {"id": 2, "user_id": "u1", "role": "free"},
    "h2": {"id": 3, "user_id": "u2", "role": "free"},
}

# Every write that names one account, with the arguments that name u1.
_ACCOUNT_WRITES: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("set_user_preference", ("u1", "llm_prober_layout", {"panes": 2}), {}),
    ("delete_user_preference", ("u1", "llm_prober_layout"), {}),
    (
        "set_user_preference_if_newer",
        ("u1", "locale", {"value": "en"}, datetime(2026, 1, 1, tzinfo=UTC)),
        {},
    ),
    ("update_user_fields", ("u1",), {"role": "pro"}),
    ("suspend_user", ("u1",), {}),
    ("delete_user", ("u1",), {}),
    ("begin_hard_delete_user", ("u1",), {}),
    ("release_hard_delete_user_claim", ("u1", "claim"), {}),
    ("resume_user", ("u1",), {}),
    ("hard_delete_user", ("u1",), {"claim_token": "claim"}),
    ("approve_user", ("u1",), {"admin_id": "admin"}),
    ("reject_user", ("u1",), {"admin_id": "admin", "reason": "spam"}),
    ("update_key", ("u1",), {"quota_daily_cost_usd": 1.0}),
    ("revoke_key", ("u1",), {}),
    ("regenerate_key", ("u1",), {"new_key_hash": "n", "new_key_prefix": "p"}),
    (
        "rotate_key",
        ("u1", "pfx"),
        {"new_key_hash": "n", "new_key_prefix": "p", "api_key_encrypted": None},
    ),
    ("update_key_settings", ("u1", "pfx", {"budget_daily_usd": 2}), {}),
    ("create_key", (), {"key_hash": "n", "key_prefix": "p", "user_id": "u1"}),
    ("bind_user_phone", ("u1", "+15550100"), {}),
    ("bind_user_email", ("u1", "a@example.test"), {}),
    ("mark_user_email_verified", ("u1",), {}),
]


@pytest.fixture
def inner() -> MagicMock:
    store = MagicMock()
    store.get_auth_context_by_key_hash = AsyncMock(side_effect=lambda h: dict(_ROWS[h]))
    store.get_auth_context_lightweight = AsyncMock(side_effect=lambda h: dict(_ROWS[h]))
    store.get_key_owner_for_audit = AsyncMock(
        side_effect=lambda h: {**_ROWS[h], "key_status": "active", "key_expired": False}
    )
    store.session_is_live = AsyncMock(return_value=True)
    store.update_key_settings = AsyncMock(return_value=({}, {}))
    store.bind_user_phone = AsyncMock(return_value=True)
    store.bind_user_email = AsyncMock(return_value=True)
    store.apply_role_quota = AsyncMock(return_value=3)
    store.attach_browser_session_account = AsyncMock(return_value=True)
    for name, _, _ in _ACCOUNT_WRITES:
        if not isinstance(getattr(store, name, None), AsyncMock):
            setattr(store, name, AsyncMock())
    return store


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def cached(inner, cache) -> CachedOperationalStore:
    return CachedOperationalStore(inner, cache)


async def _warm(cached: CachedOperationalStore) -> None:
    for key_hash in _ROWS:
        await cached.get_auth_context_by_key_hash(key_hash)
        await cached.get_auth_context_lightweight(key_hash)
        await cached.get_key_owner_for_audit(key_hash)


def _lookups(inner: MagicMock) -> int:
    return (
        inner.get_auth_context_by_key_hash.await_count
        + inner.get_auth_context_lightweight.await_count
        + inner.get_key_owner_for_audit.await_count
    )


@pytest.mark.parametrize(("write", "args", "kwargs"), _ACCOUNT_WRITES, ids=lambda v: str(v)[:40])
async def test_an_accounts_write_evicts_only_its_own_auth(
    cached, inner, cache, write, args, kwargs
):
    await _warm(cached)
    assert _lookups(inner) == 9
    scans = AsyncMock(wraps=cache.delete_pattern)
    cache.delete_pattern = scans

    await getattr(cached, write)(*args, **kwargs)

    # Both of u1's keys are read again, through all three lookups ...
    for key_hash in ("h1", "h1b"):
        await cached.get_auth_context_by_key_hash(key_hash)
        await cached.get_auth_context_lightweight(key_hash)
        await cached.get_key_owner_for_audit(key_hash)
    assert _lookups(inner) == 9 + 6
    # ... and u2's are still answered from the cache.
    await cached.get_auth_context_by_key_hash("h2")
    await cached.get_auth_context_lightweight("h2")
    await cached.get_key_owner_for_audit("h2")
    assert _lookups(inner) == 9 + 6
    # No write about one account scans the auth namespaces.
    assert not [c for c in scans.await_args_list if c.args[0].startswith("auth")]


async def test_saving_a_preference_keeps_other_accounts_cached(cached, inner):
    """The reported case: one user saving a layout flushed every user's auth."""
    await _warm(cached)

    await cached.set_user_preference("u2", "llm_prober_layout", {"panes": 3})

    await cached.get_auth_context_by_key_hash("h1")
    await cached.get_auth_context_by_key_hash("h1b")
    assert inner.get_auth_context_by_key_hash.await_count == 3
    await cached.get_auth_context_by_key_hash("h2")
    assert inner.get_auth_context_by_key_hash.await_count == 4


async def test_a_change_to_every_account_still_flushes_every_account(cached, inner):
    await _warm(cached)

    await cached.apply_role_quota("free", Decimal("5"))

    for key_hash in _ROWS:
        await cached.get_auth_context_by_key_hash(key_hash)
    assert inner.get_auth_context_by_key_hash.await_count == 6


async def test_an_account_id_is_matched_exactly(cached, inner):
    rows = {"a": {"id": 1, "user_id": "u*"}, "b": {"id": 2, "user_id": "u1"}}
    inner.get_auth_context_by_key_hash = AsyncMock(side_effect=lambda h: dict(rows[h]))
    await cached.get_auth_context_by_key_hash("a")
    await cached.get_auth_context_by_key_hash("b")

    await cached.invalidate_user_auth_caches("u*")

    await cached.get_auth_context_by_key_hash("b")
    assert inner.get_auth_context_by_key_hash.await_count == 2


async def test_a_row_naming_no_account_is_not_cached(cached, inner):
    """No account's invalidation could reach it, so it is read every time."""
    inner.get_auth_context_by_key_hash = AsyncMock(return_value={"id": 9})

    await cached.get_auth_context_by_key_hash("orphan")
    await cached.get_auth_context_by_key_hash("orphan")

    assert inner.get_auth_context_by_key_hash.await_count == 2


async def test_an_invalidation_racing_a_fill_wins(cached, inner):
    """A lookup that read before the write must not publish after it."""

    async def read_then_changed(key_hash):
        await cached.invalidate_user_auth_caches("u1")
        return dict(_ROWS[key_hash])

    inner.get_auth_context_by_key_hash = AsyncMock(side_effect=read_then_changed)
    await cached.get_auth_context_by_key_hash("h1")

    inner.get_auth_context_by_key_hash = AsyncMock(side_effect=lambda h: dict(_ROWS[h]))
    await cached.get_auth_context_by_key_hash("h1")
    inner.get_auth_context_by_key_hash.assert_awaited_once()


async def test_a_sign_in_forgets_the_accounts_sessions_without_a_scan(cached, inner, cache):
    await cached.session_is_live("u1", "s1")
    await cached.session_is_live("u2", "s2")
    scans = AsyncMock(wraps=cache.delete_pattern)
    cache.delete_pattern = scans

    await cached.attach_browser_session_account("browser", user_id="u1", auth_session_id="s")

    scans.assert_not_awaited()
    await cached.session_is_live("u1", "s1")
    await cached.session_is_live("u2", "s2")
    assert inner.session_is_live.await_count == 3


# ---------------------------------------------------------------------------
# The tag index itself
# ---------------------------------------------------------------------------


async def test_delete_tag_removes_exactly_the_tagged_keys():
    cache = InMemoryCache()
    await cache.set("a", 1, 30, tags=("t1",))
    await cache.set("b", 2, 30, tags=("t1", "t2"))
    await cache.set("c", 3, 30, tags=("t2",))
    await cache.set("d", 4, 30)
    generation = cache.current_generation()

    await cache.delete_tag("t1")

    assert [await cache.get(k) for k in "abcd"] == [None, None, 3, 4]
    assert cache.current_generation() == generation + 1
    # b's other tag no longer names it.
    await cache.delete_tag("t2")
    assert await cache.get("d") == 4
    assert cache._tag_keys == {} and set(cache._key_tags) == set()


async def test_delete_tag_of_an_unknown_tag_still_fences_a_racing_fill():
    cache = InMemoryCache()
    generation = cache.current_generation()

    await cache.delete_tag("nobody")

    assert not await cache.set_if_generation("k", 1, 30, generation=generation, tags=("t",))
    assert await cache.get("k") is None


async def test_an_overwrite_keeps_only_the_new_tags():
    cache = InMemoryCache()
    await cache.set("k", 1, 30, tags=("old",))
    await cache.set("k", 2, 30, tags=("new",))

    await cache.delete_tag("old")
    assert await cache.get("k") == 2
    await cache.delete_tag("new")
    assert await cache.get("k") is None


async def test_the_index_never_outgrows_the_cache():
    """Eviction, expiry and deletes all leave the index with their entries."""
    cache = InMemoryCache(max_entries=8)
    for i in range(50):
        await cache.set(f"k{i}", i, 30, tags=(f"user:{i % 5}", "all"))
    assert len(cache._store) == 8
    assert set(cache._key_tags) == set(cache._store)
    assert sum(len(keys) for keys in cache._tag_keys.values()) == 2 * 8

    await cache.delete("k49")
    await cache.delete_pattern("k4[23]")
    assert sorted(cache._store) == ["k44", "k45", "k46", "k47", "k48"]
    assert set(cache._key_tags) == set(cache._store)
    assert cache._tag_keys["all"] == set(cache._store)


async def test_expired_entries_leave_the_index(monkeypatch):
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    cache = InMemoryCache()
    await cache.set("a", 1, 5, tags=("t",))
    await cache.set("b", 2, 5, tags=("t",))
    clock[0] += 10

    assert await cache.get("a") is None  # expired on read
    assert await cache.get("missing") is None  # a miss sweeps the rest

    assert cache._store == {} and cache._tag_keys == {} and cache._key_tags == {}


async def test_a_miss_sweeps_a_bounded_window(monkeypatch):
    """A miss inspects the oldest few entries, never the whole map."""
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    cache = InMemoryCache(max_entries=1000)
    for i in range(600):
        await cache.set(f"k{i}", i, 5, tags=("t",))
    clock[0] += 10

    assert await cache.get("missing") is None

    # The default window is 256: the rest stay until read or swept later.
    assert len(cache._store) == 600 - 256
    assert set(cache._key_tags) == set(cache._store)


@pytest.mark.parametrize(
    ("write", "args"),
    [("revoke_session", ("auth-session-1",)), ("revoke_browser_session", ("browser-1",))],
)
async def test_a_revocation_naming_no_account_forgets_sessions_without_a_scan(
    cached, inner, cache, write, args
):
    """A sign-out names a session, not an account: every session entry goes, via the index."""
    setattr(inner, write, AsyncMock())
    await cached.session_is_live("u1", "s1")
    await cached.session_is_live("u2", "s2")
    await cached.get_auth_context_by_key_hash("h1")
    scans = AsyncMock(wraps=cache.delete_pattern)
    cache.delete_pattern = scans

    await getattr(cached, write)(*args)

    scans.assert_not_awaited()
    await cached.session_is_live("u1", "s1")
    await cached.session_is_live("u2", "s2")
    assert inner.session_is_live.await_count == 4
    # Auth entries are not sessions: they stay.
    await cached.get_auth_context_by_key_hash("h1")
    assert inner.get_auth_context_by_key_hash.await_count == 1
