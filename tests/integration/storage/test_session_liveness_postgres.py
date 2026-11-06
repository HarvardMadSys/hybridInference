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
