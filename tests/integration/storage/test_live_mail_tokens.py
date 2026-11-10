"""The live-link lookups the mail routes resend instead of minting (Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

USER = "user_live_mail_tokens"
OTHER = "user_live_mail_tokens_other"


@pytest_asyncio.fixture
async def store():
    """A PostgresOperationalStore with no tokens for the two test accounts."""
    pool = await postgres.create_pool(min_size=1, max_size=4)
    op_store = PostgresOperationalStore(pool)
    await op_store.initialize()

    async def clean() -> None:
        async with pool.acquire() as conn:
            for table in ("password_reset_tokens", "email_verification_tokens"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE user_id = ANY($1::text[])", [USER, OTHER]
                )

    await clean()
    try:
        yield op_store
    finally:
        await clean()
        await pool.close()


async def test_the_newest_reset_link_valid_long_enough_is_returned(store):
    now = datetime.now(UTC)
    await store.create_reset_token(
        token="reset-short", user_id=USER, expires_at=now + timedelta(minutes=10)
    )
    await store.create_reset_token(
        token="reset-long", user_id=USER, expires_at=now + timedelta(minutes=50)
    )
    await store.create_reset_token(
        token="reset-other", user_id=OTHER, expires_at=now + timedelta(minutes=59)
    )

    assert await store.get_live_reset_token(USER, valid_until=now + timedelta(minutes=30)) == (
        "reset-long"
    )
    assert await store.get_live_reset_token(USER, valid_until=now + timedelta(minutes=55)) is None


async def test_a_used_reset_link_is_never_returned(store):
    now = datetime.now(UTC)
    await store.create_reset_token(
        token="reset-used", user_id=USER, expires_at=now + timedelta(minutes=50)
    )
    await store.mark_reset_used("reset-used")

    assert await store.get_live_reset_token(USER, valid_until=now) is None


async def test_only_an_unused_signup_verification_link_is_returned(store):
    now = datetime.now(UTC)
    await store.create_verification_token(
        token="verify-binding",
        user_id=USER,
        expires_at=now + timedelta(hours=23),
        pending_email="new-address@example.com",
    )
    await store.create_verification_token(
        token="verify-used", user_id=USER, expires_at=now + timedelta(hours=23)
    )
    await store.mark_verification_used("verify-used")

    assert await store.get_live_verification_token(USER, valid_until=now) is None

    await store.create_verification_token(
        token="verify-live", user_id=USER, expires_at=now + timedelta(hours=20)
    )

    assert await store.get_live_verification_token(USER, valid_until=now + timedelta(hours=12)) == (
        "verify-live"
    )
    assert (
        await store.get_live_verification_token(USER, valid_until=now + timedelta(hours=21)) is None
    )
