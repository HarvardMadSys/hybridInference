"""Session liveness: cached briefly, and forgotten on every revocation made here.

``get_current_user`` asks ``session_is_live`` on every request a console token
makes. The answer is cached for seconds, so the hot path rarely reaches the
database, and every write through ``CachedOperationalStore`` that revokes or
deletes sessions drops what it affects, so a sign-out in this process takes
effect on the very next request rather than when the entry expires.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from serving.storage import cache as cache_module
from serving.storage.cache import CachedOperationalStore, InMemoryCache

pytestmark = pytest.mark.asyncio


@pytest.fixture
def inner() -> MagicMock:
    store = MagicMock()
    store.session_is_live = AsyncMock(return_value=True)
    for name in (
        "revoke_session",
        "delete_user_sessions",
        "revoke_user_sessions",
        "revoke_session_by_sid",
        "attach_browser_session_account",
        "detach_browser_session_account",
        "revoke_browser_session",
        "suspend_user",
        "delete_user",
        "hard_delete_user",
    ):
        setattr(store, name, AsyncMock())
    return store


@pytest.fixture
def cached(inner) -> CachedOperationalStore:
    return CachedOperationalStore(inner, InMemoryCache())


def test_the_ttl_is_seconds():
    assert 15 <= cache_module._SESSION_TTL <= 30


async def test_an_answer_is_reused_within_the_ttl(cached, inner):
    assert await cached.session_is_live("u1", "sess_a") is True
    assert await cached.session_is_live("u1", "sess_a") is True

    inner.session_is_live.assert_awaited_once_with("u1", "sess_a")


async def test_an_ended_session_is_cached_too(cached, inner):
    inner.session_is_live.return_value = False

    assert await cached.session_is_live("u1", "sess_a") is False
    assert await cached.session_is_live("u1", "sess_a") is False

    inner.session_is_live.assert_awaited_once()


async def test_the_answer_expires(cached, inner, monkeypatch):
    clock = [time.monotonic()]
    monkeypatch.setattr(InMemoryCache, "_expired", lambda _self, expires_at: clock[0] > expires_at)
    await cached.session_is_live("u1", "sess_a")

    clock[0] += cache_module._SESSION_TTL + 1
    inner.session_is_live.return_value = False

    assert await cached.session_is_live("u1", "sess_a") is False
    assert inner.session_is_live.await_count == 2


@pytest.mark.parametrize(
    ("write", "args", "kwargs"),
    [
        ("delete_user_sessions", ("u1",), {}),
        ("revoke_user_sessions", ("u1",), {"except_sid": "sess_keep"}),
        ("revoke_session_by_sid", ("u1", "sess_a"), {}),
        ("detach_browser_session_account", ("browser-1", "u1"), {}),
        (
            "attach_browser_session_account",
            ("browser-1",),
            {"user_id": "u1", "auth_session_id": "s"},
        ),
        ("suspend_user", ("u1",), {}),
        ("delete_user", ("u1",), {}),
        ("hard_delete_user", ("u1",), {"claim_token": "claim"}),
    ],
)
async def test_an_accounts_revocation_forgets_its_sessions_only(cached, inner, write, args, kwargs):
    await cached.session_is_live("u1", "sess_a")
    await cached.session_is_live("u2", "sess_b")
    inner.session_is_live.reset_mock()
    inner.session_is_live.return_value = False

    await getattr(cached, write)(*args, **kwargs)

    # The next request after the sign-out is refused, without waiting out the TTL.
    assert await cached.session_is_live("u1", "sess_a") is False
    inner.session_is_live.assert_awaited_once_with("u1", "sess_a")
    # Another account's sessions are still answered from the cache.
    assert await cached.session_is_live("u2", "sess_b") is True
    assert inner.session_is_live.await_count == 1


@pytest.mark.parametrize(
    ("write", "args", "revoked"),
    [
        ("revoke_session", ("auth-session-1",), "u1"),
        # Signing out of every account in one browser: any user may ask for it.
        ("revoke_browser_session", ("browser-1",), ["u1"]),
    ],
)
async def test_a_revocation_forgets_only_the_accounts_it_revoked(
    cached, inner, write, args, revoked
):
    getattr(inner, write).return_value = revoked
    await cached.session_is_live("u1", "sess_a")
    await cached.session_is_live("u2", "sess_b")
    inner.session_is_live.reset_mock()
    inner.session_is_live.return_value = False

    assert await getattr(cached, write)(*args) == revoked

    assert await cached.session_is_live("u1", "sess_a") is False
    inner.session_is_live.assert_awaited_once_with("u1", "sess_a")
    # Another account's sessions are still answered from the cache.
    assert await cached.session_is_live("u2", "sess_b") is True
    assert inner.session_is_live.await_count == 1


async def test_a_browser_with_several_accounts_forgets_each_of_them(cached, inner):
    inner.revoke_browser_session.return_value = ["u1", "u2"]
    for user in ("u1", "u2", "u3"):
        await cached.session_is_live(user, f"sess_{user}")
    inner.session_is_live.reset_mock()

    await cached.revoke_browser_session("browser-1")

    for user in ("u1", "u2", "u3"):
        await cached.session_is_live(user, f"sess_{user}")
    assert [c.args[0] for c in inner.session_is_live.await_args_list] == ["u1", "u2"]


@pytest.mark.parametrize(
    ("write", "args"),
    [("revoke_session", ("auth-session-1",)), ("revoke_browser_session", ("browser-1",))],
)
async def test_a_revocation_that_names_no_account_forgets_every_session(cached, inner, write, args):
    """A store that cannot say whose sessions they were: every account's, to be safe."""
    getattr(inner, write).return_value = None
    await cached.session_is_live("u1", "sess_a")
    await cached.session_is_live("u2", "sess_b")
    inner.session_is_live.return_value = False

    await getattr(cached, write)(*args)

    assert await cached.session_is_live("u1", "sess_a") is False
    assert await cached.session_is_live("u2", "sess_b") is False
    assert inner.session_is_live.await_count == 4


async def test_writes_still_return_what_the_store_returned(cached, inner):
    inner.revoke_user_sessions.return_value = 3
    inner.detach_browser_session_account.return_value = 1
    inner.attach_browser_session_account.return_value = False

    assert await cached.revoke_user_sessions("u1", except_sid=None) == 3
    assert await cached.detach_browser_session_account("browser-1", "u1") == 1
    assert (
        await cached.attach_browser_session_account("browser-1", user_id="u1", auth_session_id="s")
        is False
    )


async def test_a_revocation_racing_a_lookup_wins(cached, inner):
    """A lookup that read 'live' before the revocation must not cache it after."""

    async def read_then_revoked(user_id, sid):
        # The sign-out commits while this lookup is in flight.
        await cached.revoke_user_sessions(user_id)
        return True

    inner.session_is_live = AsyncMock(side_effect=read_then_revoked)
    assert await cached.session_is_live("u1", "sess_a") is True

    inner.session_is_live = AsyncMock(return_value=False)
    assert await cached.session_is_live("u1", "sess_a") is False


async def test_an_account_id_is_matched_literally(cached, inner):
    await cached.session_is_live("u[1]", "sess_a")
    await cached.session_is_live("u1", "sess_b")
    inner.session_is_live.reset_mock()

    await cached.revoke_user_sessions("u[1]")

    await cached.session_is_live("u1", "sess_b")
    inner.session_is_live.assert_not_awaited()
