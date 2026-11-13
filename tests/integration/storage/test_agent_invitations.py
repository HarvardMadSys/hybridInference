"""Integration tests for agent invitations (Postgres).

The properties that protect this flow are transactional: a link is claimed
exactly once even under concurrency, and a refusal after the claim writes
nothing — no user, no permanent API key, no consumed token. Only a real
database can show that; a fake store honours it because it was written to.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from serving.exceptions import AgentInviteSubjectUnavailableError, APIKeyLimitReachedError
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password, verify_password
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

EMAIL_DOMAIN = "invite.test"
PASSWORD_HASH = hash_password("correct horse battery staple 1")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _cleanup(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM agent_invite_tokens")
        await conn.execute("DELETE FROM agent_provisioned_accounts")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN (SELECT id FROM users WHERE email LIKE $1)",
            f"%@{EMAIL_DOMAIN}",
        )
        await conn.execute(
            "DELETE FROM auth_sessions WHERE user_id IN (SELECT id FROM users WHERE email LIKE $1)",
            f"%@{EMAIL_DOMAIN}",
        )
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{EMAIL_DOMAIN}")


@pytest_asyncio.fixture
async def store_and_pool():
    """Provide a clean PostgresOperationalStore for invitation tests."""
    pool = await postgres.create_pool(min_size=4, max_size=16)
    store = PostgresOperationalStore(pool)
    await store.initialize()
    await _cleanup(pool)
    try:
        yield store, pool
    finally:
        await _cleanup(pool)
        await pool.close()


async def _mint(
    store: PostgresOperationalStore, token: str, *, email: str, agent_id: str, ttl: int = 3600
) -> None:
    await store.create_agent_invite_token(
        token_hash=_hash(token),
        agent_account_id=agent_id,
        email=email,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
    )


async def _redeem(
    store: PostgresOperationalStore, token: str, *, max_keys: int = 100, key: str = "key-a"
):
    return await store.redeem_agent_invite(
        token_hash=_hash(token),
        user_id=generate_ulid(),
        user_name="Invited User",
        password_hash=PASSWORD_HASH,
        api_key_hash=hashlib.sha256(f"hyi-{key}".encode()).hexdigest(),
        api_key_prefix=f"hyi-{key}"[:12],
        credential_scope="agent",
        quota_daily_cost_usd=Decimal("100"),
        max_active_keys=max_keys,
    )


async def test_redeem_creates_user_org_and_one_scoped_key(store_and_pool):
    store, pool = store_and_pool
    await _mint(store, "tok-create", email="New@Invite.Test", agent_id="agent-1")

    row = await _redeem(store, "tok-create")

    assert row is not None
    assert row["created"] is True
    user = await store.get_user_by_email("new@invite.test")
    assert user is not None
    assert user["id"] == row["user_id"]
    assert user["status"] == "active"
    assert user["email_verified"] is True
    assert verify_password("correct horse battery staple 1", user["password_hash"])
    async with pool.acquire() as conn:
        keys = await conn.fetch(
            "SELECT credential_scope, status FROM api_keys WHERE user_id = $1", row["user_id"]
        )
        org = await conn.fetchval(
            "SELECT id FROM organizations WHERE id = $1", f"org_p_{row['user_id']}"
        )
        project = await conn.fetchval(
            "SELECT id FROM projects WHERE id = $1", f"proj_d_{row['user_id']}"
        )
    assert [(k["credential_scope"], k["status"]) for k in keys] == [("agent", "active")]
    assert org is not None and project is not None
    link = await store.get_agent_provisioned_account("agent-1")
    assert link is not None and link["user_id"] == row["user_id"] and link["revoked_at"] is None


async def test_second_redeem_of_same_token_finds_nothing(store_and_pool):
    store, _pool = store_and_pool
    await _mint(store, "tok-once", email="once@invite.test", agent_id="agent-2")
    assert await _redeem(store, "tok-once") is not None
    assert await _redeem(store, "tok-once", key="key-b") is None


async def test_expired_and_unknown_tokens_are_not_claimable(store_and_pool):
    store, _pool = store_and_pool
    await _mint(store, "tok-old", email="old@invite.test", agent_id="agent-3", ttl=-1)
    assert await _redeem(store, "tok-old") is None
    assert await _redeem(store, "never-issued") is None


async def test_minting_supersedes_the_earlier_link(store_and_pool):
    store, _pool = store_and_pool
    await _mint(store, "tok-first", email="swap@invite.test", agent_id="agent-4")
    await _mint(store, "tok-second", email="swap@invite.test", agent_id="agent-4")
    assert await _redeem(store, "tok-first") is None
    assert await _redeem(store, "tok-second") is not None


async def test_concurrent_redemptions_produce_exactly_one_account(store_and_pool):
    """The reason the claim is one conditional UPDATE inside the transaction.

    Ten simultaneous redemptions of one link must yield one account, one
    scoped key and nine refusals — not one of anything per winner.
    """
    store, pool = store_and_pool
    await _mint(store, "tok-race", email="race@invite.test", agent_id="agent-5")

    results = await asyncio.gather(
        *(_redeem(store, "tok-race", key=f"race-{i}") for i in range(10)),
        return_exceptions=True,
    )

    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1
    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE email = 'race@invite.test'")
        scoped = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE credential_scope = 'agent' AND status = 'active'"
        )
    assert users == 1
    assert scoped == 1


async def test_existing_user_gets_password_set_and_sessions_revoked(store_and_pool):
    store, pool = store_and_pool
    user_id = generate_ulid()
    await store.create_user(
        user_id=user_id,
        email="member@invite.test",
        password_hash=hash_password("old-password-123"),
        user_name="Member",
        email_verified=True,
        status="active",
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, jti, sid, expires_at) "
            "VALUES ($1, $2, 'hash', 'jti', 'sid', NOW() + INTERVAL '1 day')",
            generate_ulid(),
            user_id,
        )
    await _mint(store, "tok-member", email="member@invite.test", agent_id="agent-6")

    row = await _redeem(store, "tok-member")

    assert row is not None and row["created"] is False and row["user_id"] == user_id
    user = await store.get_user_by_id(user_id)
    assert verify_password("correct horse battery staple 1", user["password_hash"])
    assert not verify_password("old-password-123", user["password_hash"])
    async with pool.acquire() as conn:
        live = await conn.fetchval(
            "SELECT COUNT(*) FROM auth_sessions WHERE user_id = $1 AND revoked IS NOT TRUE",
            user_id,
        )
    assert live == 0


async def test_suspended_account_refuses_and_token_survives(store_and_pool):
    store, _pool = store_and_pool
    user_id = generate_ulid()
    await store.create_user(
        user_id=user_id,
        email="banned@invite.test",
        password_hash=hash_password("old-password-123"),
        user_name="Banned",
        email_verified=True,
        status="suspended",
    )
    await _mint(store, "tok-banned", email="banned@invite.test", agent_id="agent-7")

    with pytest.raises(AgentInviteSubjectUnavailableError):
        await _redeem(store, "tok-banned")

    # Nothing was written and the same link works once an admin restores the
    # account: the sanction, not the invitation, was the refusal.
    user = await store.get_user_by_id(user_id)
    assert user["status"] == "suspended"
    assert verify_password("old-password-123", user["password_hash"])
    await store.update_user_fields(user_id, status="active")
    assert await _redeem(store, "tok-banned") is not None


async def test_key_ceiling_refuses_and_token_survives(store_and_pool):
    store, pool = store_and_pool
    user_id = generate_ulid()
    await store.create_user(
        user_id=user_id,
        email="full@invite.test",
        password_hash=hash_password("old-password-123"),
        user_name="Full",
        email_verified=True,
        status="active",
    )
    await store.create_key(
        key_hash=hashlib.sha256(b"hyi-existing").hexdigest(),
        key_prefix="hyi-existing"[:12],
        user_id=user_id,
        inherit_account_quota=True,
    )
    await _mint(store, "tok-full", email="full@invite.test", agent_id="agent-8")

    with pytest.raises(APIKeyLimitReachedError):
        await _redeem(store, "tok-full", max_keys=1)

    async with pool.acquire() as conn:
        scoped = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = $1 AND credential_scope = 'agent'",
            user_id,
        )
    assert scoped == 0
    # The link was not consumed: with room under the ceiling it redeems.
    assert await _redeem(store, "tok-full", key="key-c") is not None


async def test_reinvitation_replaces_the_scoped_key(store_and_pool):
    store, pool = store_and_pool
    await _mint(store, "tok-v1", email="again@invite.test", agent_id="agent-9")
    first = await _redeem(store, "tok-v1", key="v1")
    await _mint(store, "tok-v2", email="again@invite.test", agent_id="agent-9")

    second = await _redeem(store, "tok-v2", key="v2")

    assert second is not None and second["user_id"] == first["user_id"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status FROM api_keys WHERE user_id = $1 AND credential_scope = 'agent' "
            "ORDER BY created_at",
            first["user_id"],
        )
    assert [r["status"] for r in rows] == ["revoked", "active"]


async def test_revoke_kills_the_scoped_key_and_is_idempotent(store_and_pool):
    store, pool = store_and_pool
    await _mint(store, "tok-revoke", email="gone@invite.test", agent_id="agent-10")
    row = await _redeem(store, "tok-revoke")

    assert await store.revoke_agent_provisioned_account("agent-10") is not None
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM api_keys WHERE id = $1", row["api_key_id"])
    assert status == "revoked"
    assert await store.revoke_agent_provisioned_account("agent-10") is not None
    assert await store.revoke_agent_provisioned_account("never-provisioned") is None
