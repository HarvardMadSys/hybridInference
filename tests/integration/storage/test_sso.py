"""Integration tests for single sign-on storage (Postgres).

What protects the sign-in path is transactional: a flow, a pending link and a
re-authentication are each spent exactly once even under concurrency, an
identity signs in to one account only, and an account is never left with no
way in. Only a real database can show that.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from serving.storage import sso
from serving.storage.postgres_operational import PostgresOperationalStore
from serving.utils.jwt import generate_ulid
from serving.utils.password import hash_password
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

EMAIL_DOMAIN = "sso.test"
ISSUER = "https://idp.sso.test/realms/main"
PASSWORD_HASH = hash_password("correct horse battery staple 1")


async def _cleanup(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sso_pending_links WHERE email LIKE $1", f"%@{EMAIL_DOMAIN}")
        await conn.execute("DELETE FROM sso_flows WHERE provider_id = 'sso-test'")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN (SELECT id FROM users WHERE email LIKE $1)",
            f"%@{EMAIL_DOMAIN}",
        )
        # The identities leave with their accounts.
        await conn.execute("DELETE FROM users WHERE email LIKE $1", f"%@{EMAIL_DOMAIN}")


@pytest_asyncio.fixture
async def store_and_pool():
    """Provide a clean PostgresOperationalStore for single sign-on tests."""
    pool = await postgres.create_pool(min_size=4, max_size=16)
    store = PostgresOperationalStore(pool)
    await store.initialize()
    await _cleanup(pool)
    try:
        yield store, pool
    finally:
        await _cleanup(pool)
        await pool.close()


async def _user(store, name: str, *, password: bool = True, status: str = "active") -> str:
    user_id = generate_ulid()
    await store.create_user(
        user_id=user_id,
        email=f"{name}@{EMAIL_DOMAIN}",
        password_hash=PASSWORD_HASH if password else None,
        email_verified=True,
        status=status,
    )
    return user_id


async def _link(store, user_id: str, subject: str, *, issuer: str = ISSUER) -> str:
    return await store.link_user_identity(
        identity_id=generate_ulid(),
        user_id=user_id,
        provider_id="sso-test",
        issuer=issuer,
        subject=subject,
        email=f"{subject}@{EMAIL_DOMAIN}",
    )


async def _flow(store, state_hash: str, *, purpose: str = "login", ttl: int = 600, **extra) -> None:
    await store.create_sso_flow(
        state_hash=state_hash,
        binding_hash="binding",
        provider_id="sso-test",
        purpose=purpose,
        return_to="/dashboard",
        protocol_state={"nonce_hash": "n", "code_verifier": "v"},
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        **extra,
    )


# -- identities ---------------------------------------------------------------


async def test_a_linked_identity_is_found_by_issuer_and_subject(store_and_pool):
    store, _pool = store_and_pool
    user_id = await _user(store, "alice")

    assert await _link(store, user_id, "alice") == sso.LINKED

    identity = await store.get_user_identity(ISSUER, "alice")
    assert identity["user_id"] == user_id
    assert identity["email"] == f"alice@{EMAIL_DOMAIN}"
    assert identity["last_login_at"] is None
    assert await store.get_user_identity(ISSUER, "nobody") is None
    assert [row["id"] for row in await store.list_user_identities(user_id)] == [identity["id"]]

    await store.touch_user_identity(identity["id"])
    assert (await store.get_user_identity(ISSUER, "alice"))["last_login_at"] is not None


async def test_linking_says_why_it_refuses(store_and_pool):
    store, _pool = store_and_pool
    alice = await _user(store, "alice")
    bob = await _user(store, "bob")
    suspended = await _user(store, "carol", status="suspended")
    assert await _link(store, alice, "alice") == sso.LINKED

    # The same identity again is the same link, not an error.
    assert await _link(store, alice, "alice") == sso.LINKED
    # An identity signs in to one account only.
    assert await _link(store, bob, "alice") == sso.IDENTITY_IN_USE
    # An account holds one identity per issuer.
    assert await _link(store, alice, "alice-recreated") == sso.ISSUER_CONFLICT
    # Another issuer is another identity.
    assert await _link(store, alice, "alice", issuer="https://other.sso.test") == sso.LINKED
    # Nothing is linked to an account that is not active, or does not exist.
    assert await _link(store, suspended, "carol") == sso.ACCOUNT_INACTIVE
    assert await _link(store, "no-such-user", "dave") == sso.ACCOUNT_INACTIVE
    assert await store.get_user_identity(ISSUER, "carol") is None


async def test_concurrent_links_of_one_identity_give_it_to_one_account(store_and_pool):
    store, _pool = store_and_pool
    users = [await _user(store, f"racer{index}") for index in range(8)]

    outcomes = await asyncio.gather(*(_link(store, user_id, "shared") for user_id in users))

    assert sorted(outcomes) == [sso.IDENTITY_IN_USE] * 7 + [sso.LINKED]
    winner = users[outcomes.index(sso.LINKED)]
    assert (await store.get_user_identity(ISSUER, "shared"))["user_id"] == winner


async def test_unlinking_never_removes_the_last_way_in(store_and_pool):
    store, pool = store_and_pool
    with_password = await _user(store, "pw")
    sso_only = await _user(store, "ssoonly", password=False)
    await _link(store, with_password, "pw")
    await _link(store, sso_only, "ssoonly")
    pw_identity = (await store.get_user_identity(ISSUER, "pw"))["id"]
    only_identity = (await store.get_user_identity(ISSUER, "ssoonly"))["id"]

    # An account with a password may drop its identity.
    assert (
        await store.unlink_user_identity(
            identity_id=pw_identity, user_id=with_password, keep_last_sign_in=True
        )
        == sso.UNLINKED
    )
    # One with nothing else may not, unless the caller says so (an administrator).
    assert (
        await store.unlink_user_identity(
            identity_id=only_identity, user_id=sso_only, keep_last_sign_in=True
        )
        == sso.LAST_SIGN_IN
    )
    assert await store.get_user_identity(ISSUER, "ssoonly") is not None

    # A second identity, or a verified phone, is another way in.
    await _link(store, sso_only, "ssoonly", issuer="https://other.sso.test")
    assert (
        await store.unlink_user_identity(
            identity_id=only_identity, user_id=sso_only, keep_last_sign_in=True
        )
        == sso.UNLINKED
    )
    other = (await store.get_user_identity("https://other.sso.test", "ssoonly"))["id"]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET phone = '+8613800000000', phone_verified = TRUE WHERE id = $1",
            sso_only,
        )
    assert (
        await store.unlink_user_identity(
            identity_id=other, user_id=sso_only, keep_last_sign_in=True
        )
        == sso.UNLINKED
    )
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET phone = NULL WHERE id = $1", sso_only)


async def test_unlinking_is_scoped_to_the_account_and_an_administrator_may_remove_the_last(
    store_and_pool,
):
    store, _pool = store_and_pool
    alice = await _user(store, "alice", password=False)
    bob = await _user(store, "bob")
    await _link(store, alice, "alice")
    identity = (await store.get_user_identity(ISSUER, "alice"))["id"]

    # Another account cannot remove it, and an unknown id is not found.
    assert (
        await store.unlink_user_identity(identity_id=identity, user_id=bob, keep_last_sign_in=False)
        == sso.NOT_FOUND
    )
    assert (
        await store.unlink_user_identity(
            identity_id="no-such-identity", user_id=alice, keep_last_sign_in=False
        )
        == sso.NOT_FOUND
    )
    assert (
        await store.unlink_user_identity(
            identity_id=identity, user_id=alice, keep_last_sign_in=False
        )
        == sso.UNLINKED
    )
    assert await store.list_user_identities(alice) == []


async def test_a_first_sign_in_creates_the_account_and_its_identity_together(store_and_pool):
    store, pool = store_and_pool
    user_id = generate_ulid()

    created = await store.create_sso_user(
        user_id=user_id,
        email=f"New.Member@{EMAIL_DOMAIN}",
        user_name="New Member",
        role="pro",
        status="pending_approval",
        identity_id=generate_ulid(),
        provider_id="sso-test",
        issuer=ISSUER,
        subject="new-member",
    )

    assert created is True
    user = await store.get_user_by_id(user_id)
    assert user["email"] == f"new.member@{EMAIL_DOMAIN}"
    assert user["email_verified"] is True
    assert user["password_hash"] is None
    assert (user["role"], user["status"]) == ("pro", "pending_approval")
    assert (await store.get_user_identity(ISSUER, "new-member"))["user_id"] == user_id
    async with pool.acquire() as conn:
        default_projects = await conn.fetchval(
            "SELECT COUNT(*) FROM projects WHERE user_id = $1 AND is_default", user_id
        )
    assert default_projects == 1


async def test_a_taken_email_or_identity_creates_nothing(store_and_pool):
    store, pool = store_and_pool
    existing = await _user(store, "taken")
    await _link(store, existing, "taken")

    async def create(email: str, subject: str) -> bool:
        return await store.create_sso_user(
            user_id=generate_ulid(),
            email=email,
            user_name=None,
            role="free",
            status="active",
            identity_id=generate_ulid(),
            provider_id="sso-test",
            issuer=ISSUER,
            subject=subject,
        )

    # The email already belongs to an account.
    assert await create(f"taken@{EMAIL_DOMAIN}", "someone-else") is False
    # The identity is already linked: the new account is rolled back with it.
    assert await create(f"fresh@{EMAIL_DOMAIN}", "taken") is False
    async with pool.acquire() as conn:
        fresh = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE email = $1", f"fresh@{EMAIL_DOMAIN}"
        )
    assert fresh == 0


# -- sign-in flows ------------------------------------------------------------


async def test_a_flow_is_claimed_exactly_once(store_and_pool):
    store, _pool = store_and_pool
    await _flow(store, "state-once")

    claims = await asyncio.gather(*(store.consume_sso_flow("state-once") for _ in range(8)))

    won = [claim for claim in claims if claim is not None]
    assert len(won) == 1
    assert won[0]["purpose"] == "login"
    assert won[0]["binding_hash"] == "binding"
    assert won[0]["protocol_state"] == {"nonce_hash": "n", "code_verifier": "v"}
    assert await store.consume_sso_flow("state-once") is None
    assert await store.consume_sso_flow("no-such-state") is None


async def test_an_expired_flow_cannot_be_claimed(store_and_pool):
    store, _pool = store_and_pool
    await _flow(store, "state-expired", ttl=-1)

    assert await store.consume_sso_flow("state-expired") is None


async def test_a_reauthentication_is_spent_once_by_the_session_and_purpose_it_confirmed(
    store_and_pool,
):
    store, _pool = store_and_pool
    user_id = await _user(store, "reauth", password=False)
    other = await _user(store, "other", password=False)
    await _flow(
        store,
        "state-reauth",
        purpose="reauth",
        user_id=user_id,
        sid="sid-1",
        reauth_purpose="delete_account",
    )

    async def spend(**overrides) -> bool:
        fields = {
            "user_id": user_id,
            "sid": "sid-1",
            "purpose": "delete_account",
            "max_age_seconds": 300,
        }
        return await store.consume_sso_reauth_proof(**{**fields, **overrides})

    # Not before the identity provider answered.
    assert await spend() is False
    flow = await store.consume_sso_flow("state-reauth")
    assert (flow["user_id"], flow["sid"], flow["reauth_purpose"]) == (
        user_id,
        "sid-1",
        "delete_account",
    )
    assert await spend() is False
    await store.mark_sso_reauth_verified("state-reauth")

    # Another account, session or purpose cannot spend it, nor one that is too old.
    assert await spend(user_id=other) is False
    assert await spend(sid="sid-2") is False
    assert await spend(purpose="set_password") is False
    assert await spend(max_age_seconds=0) is False

    spends = await asyncio.gather(*(spend() for _ in range(8)))
    assert sorted(spends) == [False] * 7 + [True]
    assert await spend() is False


async def test_a_sign_in_flow_never_counts_as_a_reauthentication(store_and_pool):
    store, _pool = store_and_pool
    user_id = await _user(store, "login", password=False)
    await _flow(store, "state-login", user_id=user_id, sid="sid-1", reauth_purpose="bind_email")
    await store.consume_sso_flow("state-login")

    await store.mark_sso_reauth_verified("state-login")

    assert (
        await store.consume_sso_reauth_proof(
            user_id=user_id, sid="sid-1", purpose="bind_email", max_age_seconds=300
        )
        is False
    )


# -- pending links ------------------------------------------------------------


async def test_a_pending_link_is_read_without_claiming_and_claimed_once(store_and_pool):
    store, _pool = store_and_pool
    await store.create_sso_pending_link(
        token_hash="pending-1",
        provider_id="sso-test",
        issuer=ISSUER,
        subject="alice",
        email=f"Alice@{EMAIL_DOMAIN}",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    for _ in range(2):
        pending = await store.get_sso_pending_link("pending-1")
        assert (pending["issuer"], pending["subject"]) == (ISSUER, "alice")
        assert pending["email"] == f"alice@{EMAIL_DOMAIN}"

    claims = await asyncio.gather(*(store.consume_sso_pending_link("pending-1") for _ in range(8)))
    assert len([claim for claim in claims if claim is not None]) == 1
    assert await store.get_sso_pending_link("pending-1") is None


async def test_a_declined_or_expired_pending_link_is_gone(store_and_pool):
    store, _pool = store_and_pool
    for token, minutes in (("pending-declined", 10), ("pending-expired", -1)):
        await store.create_sso_pending_link(
            token_hash=token,
            provider_id="sso-test",
            issuer=ISSUER,
            subject=token,
            email=f"{token}@{EMAIL_DOMAIN}",
            expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        )

    await store.delete_sso_pending_link("pending-declined")

    assert await store.get_sso_pending_link("pending-declined") is None
    assert await store.get_sso_pending_link("pending-expired") is None
    assert await store.consume_sso_pending_link("pending-expired") is None
