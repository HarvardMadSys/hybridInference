"""``session_is_live``: what an access token's ``sid`` is checked against (Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

USER = "user_session_liveness"
OTHER = "user_session_liveness_other"
BROWSER = "bs-session-liveness"


@pytest_asyncio.fixture
async def store():
    """A PostgresOperationalStore with no sessions for the two test accounts."""
    pool = await postgres.create_pool(min_size=1, max_size=4)
    op_store = PostgresOperationalStore(pool)
    await op_store.initialize()

    async def clean() -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = ANY($1::text[])", [USER, OTHER]
            )
            await conn.execute(
                "DELETE FROM browser_session_accounts WHERE browser_session_id = $1", BROWSER
            )
            await conn.execute("DELETE FROM browser_sessions WHERE id = $1", BROWSER)

    await clean()
    try:
        yield op_store
    finally:
        await clean()
        await pool.close()


async def _session(store, session_id: str, sid: str, *, user_id: str = USER, ttl=None) -> None:
    await store.create_session(
        session_id=session_id,
        user_id=user_id,
        refresh_token_hash=f"hash-{session_id}",
        jti=f"jti-{session_id}",
        sid=sid,
        expires_at=datetime.now(UTC) + (ttl or timedelta(days=30)),
    )


async def test_a_session_is_live_until_it_is_revoked(store):
    await _session(store, "as-1", "sess_1")

    assert await store.session_is_live(USER, "sess_1") is True
    # The sid is checked with the account it was issued to.
    assert await store.session_is_live(OTHER, "sess_1") is False
    assert await store.session_is_live(USER, "sess_unknown") is False

    await store.revoke_session("as-1")

    assert await store.session_is_live(USER, "sess_1") is False


async def test_an_expired_session_is_not_live(store):
    await _session(store, "as-old", "sess_old", ttl=timedelta(seconds=-1))

    assert await store.session_is_live(USER, "sess_old") is False


async def test_signing_out_the_other_devices_keeps_this_one(store):
    await _session(store, "as-here", "sess_here")
    await _session(store, "as-there", "sess_there")

    assert await store.revoke_user_sessions(USER, except_sid="sess_here") == 1

    assert await store.session_is_live(USER, "sess_here") is True
    assert await store.session_is_live(USER, "sess_there") is False


async def test_deleted_sessions_are_not_live(store):
    await _session(store, "as-1", "sess_1")
    await _session(store, "as-other", "sess_other", user_id=OTHER)

    await store.delete_user_sessions(USER)

    assert await store.session_is_live(USER, "sess_1") is False
    assert await store.session_is_live(OTHER, "sess_other") is True


async def test_the_lookup_is_one_probe_however_many_sessions_the_account_holds(store):
    """An account that signed in thousands of times: its sid is found, not searched for.

    Through ``(user_id, expires_at)`` the lookup read every unexpired session of
    the account to find the one sid -- on the request pool, on every request a
    console token makes.
    """
    async with store._pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, sid, expires_at) "
            "SELECT 'as-many-' || g, $1, 'hash-many-' || g, 'sess_many_' || g, "
            "NOW() + INTERVAL '30 days' FROM generate_series(1, 3000) AS g",
            USER,
        )
        await conn.execute("ANALYZE auth_sessions")
        plan = await conn.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT EXISTS (SELECT 1 FROM auth_sessions "
            "WHERE user_id = $1 AND expires_at > NOW() AND sid = $2 AND revoked IS NOT TRUE)",
            USER,
            "sess_many_1234",
        )
    text = str(plan)
    assert "idx_auth_sessions_user_sid" in text
    assert await store.session_is_live(USER, "sess_many_1234") is True


async def test_revoking_a_session_names_the_account_it_belonged_to(store):
    """So the cache forgets that account's cached liveness, and no one else's."""
    await _session(store, "as-1", "sess_1")

    assert await store.revoke_session("as-1") == USER
    assert await store.revoke_session("as-unknown") is None


async def test_signing_a_browser_out_names_the_accounts_it_signed_out(store):
    await _session(store, "as-1", "sess_1")
    await _session(store, "as-2", "sess_2", user_id=OTHER)
    await _session(store, "as-elsewhere", "sess_3")
    await store.create_browser_session(
        browser_session_id=BROWSER,
        secret_hash="hash-browser-liveness",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    for user_id, session_id in ((USER, "as-1"), (OTHER, "as-2")):
        assert await store.attach_browser_session_account(
            BROWSER, user_id=user_id, auth_session_id=session_id
        )

    assert await store.revoke_browser_session(BROWSER) == sorted([USER, OTHER])

    assert await store.session_is_live(USER, "sess_1") is False
    assert await store.session_is_live(OTHER, "sess_2") is False
    # The account's session in another browser is not this browser's to end.
    assert await store.session_is_live(USER, "sess_3") is True
