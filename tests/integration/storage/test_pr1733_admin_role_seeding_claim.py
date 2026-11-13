"""The startup role seeding must not touch an account under a claim.

``PostgresOperationalStore.initialize()`` promotes the accounts named in
``ADMIN_EMAILS`` to the admin role. It runs at every start, so a backend
restart could change an account's role while its hard-delete claim is
active -- the identity-field mutation the claim exists to prevent.

The seeding update had no ``status`` and no ``hard_delete_pending``
predicate, so it promoted a soft-deleted account and an account under a
claim alike. These tests are red on that code and green with the
predicates.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=8)
    try:
        yield pool
    finally:
        await pool.close()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _seed(pool, uid: str, *, email: str, status: str, role: str = "free") -> None:
    await pool.execute(
        "INSERT INTO users (id, email, status, role) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (id) DO UPDATE SET email = $2, status = $3, role = $4",
        uid,
        email,
        status,
        role,
    )


async def _role(pool, uid: str) -> str:
    return await pool.fetchval("SELECT role FROM users WHERE id = $1", uid)


async def _seed_admin_emails(monkeypatch, addresses: list[str]) -> None:
    """Point the setting at *addresses* for the store's initialize()."""
    from serving.config.settings import get_settings

    monkeypatch.setenv("ADMIN_EMAILS", ",".join(addresses))
    get_settings.cache_clear()


async def test_the_seeding_promotes_an_active_account(pool, monkeypatch):
    """The ordinary case is unchanged: an eligible account is promoted."""
    uid = _uid("seed-active")
    email = f"{uid}@example.com"
    await _seed(pool, uid, email=email, status="active")
    await _seed_admin_emails(monkeypatch, [email])

    await PostgresOperationalStore(pool).initialize()

    assert await _role(pool, uid) == "admin"


async def test_the_seeding_leaves_a_claimed_deleted_account_alone(pool, monkeypatch):
    """A claimed account under erasure keeps its role.

    The account is soft-deleted and under a hard-delete claim: the erasure
    is in progress, so the startup seeding must not promote it.
    """
    uid = _uid("seed-claimed")
    email = f"{uid}@example.com"
    await _seed(pool, uid, email=email, status="deleted")
    store = PostgresOperationalStore(pool)
    await store.begin_hard_delete_user(uid)
    await _seed_admin_emails(monkeypatch, [email])

    await store.initialize()

    assert await _role(pool, uid) == "free"
    # The claim is untouched by the seeding.
    row = await pool.fetchrow(
        "SELECT hard_delete_pending, hard_delete_claim_token FROM users WHERE id = $1", uid
    )
    assert row["hard_delete_pending"] is True
    assert row["hard_delete_claim_token"] is not None


async def test_the_seeding_leaves_a_deleted_account_alone(pool, monkeypatch):
    """A soft-deleted account without a claim is not promoted either.

    A deleted account is not a candidate for the admin role whatever its
    claim state: the role would give a deleted account administrator rights
    over an account that is being erased.
    """
    uid = _uid("seed-deleted")
    email = f"{uid}@example.com"
    await _seed(pool, uid, email=email, status="deleted")
    await _seed_admin_emails(monkeypatch, [email])

    await PostgresOperationalStore(pool).initialize()

    assert await _role(pool, uid) == "free"


async def test_the_seeding_still_promotes_an_eligible_account_alongside_a_claimed_one(
    pool, monkeypatch
):
    """One address in the list is claimed; the other is still promoted."""
    claimed = _uid("seed-mixed-claimed")
    free = _uid("seed-mixed-free")
    claimed_email = f"{claimed}@example.com"
    free_email = f"{free}@example.com"
    await _seed(pool, claimed, email=claimed_email, status="deleted")
    await _seed(pool, free, email=free_email, status="active")
    store = PostgresOperationalStore(pool)
    await store.begin_hard_delete_user(claimed)
    await _seed_admin_emails(monkeypatch, [claimed_email, free_email])

    await store.initialize()

    assert await _role(pool, claimed) == "free"
    assert await _role(pool, free) == "admin"
