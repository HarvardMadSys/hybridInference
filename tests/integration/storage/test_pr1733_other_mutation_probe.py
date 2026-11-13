"""Every write to the ``users`` table is refused while a claim is active.

The avatar write was found to bypass the claim guard: it did not go through
``update_user_fields``, so it inherited neither that method's guard nor any
other. The same shape was then found in the preference writers and in
``mark_user_email_verified`` — each writes the ``users`` table directly.

This file pins the whole set: a probe that reports, per mutation, whether it
wrote during a claim. It is a regression test, so the assertions below are
the contract; the report is written out for the review record.

``update_user_last_login`` is the one mutation left unguarded, on purpose:
the login route requires an ``active`` account, so a claimed account (always
soft-deleted) cannot reach it, and it writes a timestamp rather than an
identity field. It is asserted here so that a change to that decision is
deliberate.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
import pytest_asyncio

from serving.exceptions import HardDeleteStateChanged
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

#: Where the probe writes its report, for the review record.
REPORT_PATH = pathlib.Path("/tmp/pr1733_mutation_probe.txt")

_GUARDED = (
    "set_user_avatar",
    "set_user_preference",
    "delete_user_preference",
    "set_user_preference_if_newer",
    "mark_user_email_verified",
    "update_user_fields(user_name)",
    "update_user_fields(preferences)",
)


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=10)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def store(pool):
    return PostgresOperationalStore(pool)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _seed_deleted(pool, uid: str) -> None:
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'deleted') "
        "ON CONFLICT (id) DO UPDATE SET status='deleted', hard_delete_pending=FALSE, "
        "hard_delete_claim_token=NULL, hard_delete_claimed_at=NULL, "
        "hard_delete_claim_recovered=FALSE",
        uid,
        f"{uid}@example.com",
    )


async def _probe(store, pool, name: str, write, read) -> str:
    """Run one mutation against a claimed account and report what it did."""
    uid = _uid(f"probe-{name}")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)
    try:
        await write(uid)
    except HardDeleteStateChanged:
        return "refused"
    value = await read(uid)
    return "WROTE" if value else "no-op"


async def test_every_guarded_mutation_is_refused_during_a_claim(store, pool):
    """The contract: no account mutation lands while a claim is active."""
    from datetime import UTC, datetime

    outcomes: dict[str, str] = {}

    outcomes["set_user_avatar"] = await _probe(
        store,
        pool,
        "avatar",
        lambda uid: store.set_user_avatar(uid, "data:image/png;base64,AAA"),
        lambda uid: pool.fetchval("SELECT avatar_data_url FROM users WHERE id=$1", uid),
    )
    outcomes["set_user_preference"] = await _probe(
        store,
        pool,
        "pref",
        lambda uid: store.set_user_preference(uid, "k", {"v": 1}),
        lambda uid: pool.fetchval("SELECT preferences->'k' FROM users WHERE id=$1", uid),
    )
    outcomes["delete_user_preference"] = await _probe(
        store,
        pool,
        "deletepref",
        lambda uid: store.delete_user_preference(uid, "k"),
        lambda uid: pool.fetchval("SELECT preferences->'k' FROM users WHERE id=$1", uid),
    )
    outcomes["set_user_preference_if_newer"] = await _probe(
        store,
        pool,
        "prefnewer",
        lambda uid: store.set_user_preference_if_newer(
            uid, "k", {"value": "en", "chosen_at": datetime.now(UTC).isoformat()}, datetime.now(UTC)
        ),
        lambda uid: pool.fetchval("SELECT preferences->'k' FROM users WHERE id=$1", uid),
    )
    outcomes["mark_user_email_verified"] = await _probe(
        store,
        pool,
        "verified",
        lambda uid: store.mark_user_email_verified(uid),
        lambda uid: pool.fetchval("SELECT email_verified FROM users WHERE id=$1", uid),
    )
    outcomes["update_user_fields(user_name)"] = await _probe(
        store,
        pool,
        "username",
        lambda uid: store.update_user_fields(uid, user_name="x"),
        lambda uid: pool.fetchval("SELECT user_name FROM users WHERE id=$1", uid),
    )
    outcomes["update_user_fields(preferences)"] = await _probe(
        store,
        pool,
        "prefviafields",
        lambda uid: store.update_user_fields(uid, preferences='{"k":1}'),
        lambda uid: pool.fetchval("SELECT preferences->'k' FROM users WHERE id=$1", uid),
    )

    report = "\n".join(f"  {name:34s} -> {outcome}" for name, outcome in outcomes.items())
    REPORT_PATH.write_text(report + "\n")

    for name in _GUARDED:
        assert outcomes[name] == "refused", f"{name} -> {outcomes[name]}"


async def test_the_login_timestamp_is_the_documented_exception(store, pool):
    """``update_user_last_login`` is deliberately not guarded.

    The login route requires an ``active`` account, so a claimed account
    (always soft-deleted) cannot reach it, and it writes a timestamp rather
    than an identity field. This test states the decision so that changing it
    is a deliberate act.
    """
    uid = _uid("probe-lastlogin")
    await _seed_deleted(pool, uid)
    await store.begin_hard_delete_user(uid)

    await store.update_user_last_login(uid)

    row = await pool.fetchrow("SELECT last_login_at FROM users WHERE id=$1", uid)
    assert row["last_login_at"] is not None


async def test_the_ordinary_writes_still_work(store, pool):
    """Nothing changed for an account that is not being erased."""
    uid = _uid("probe-ordinary")
    await pool.execute(
        "INSERT INTO users (id, email, status) VALUES ($1, $2, 'active') "
        "ON CONFLICT (id) DO UPDATE SET status='active', hard_delete_pending=FALSE",
        uid,
        f"{uid}@example.com",
    )

    await store.set_user_avatar(uid, "data:image/png;base64,AAA")
    assert await store.set_user_preference(uid, "k", {"v": 1}) is True
    await store.mark_user_email_verified(uid)

    row = await pool.fetchrow(
        "SELECT avatar_data_url, preferences->'k' AS pref, email_verified FROM users WHERE id=$1",
        uid,
    )
    assert row["avatar_data_url"] == "data:image/png;base64,AAA"
    assert row["pref"] is not None
    assert row["email_verified"] is True


async def test_an_unclaimed_soft_deleted_account_still_writes(store, pool):
    """The guard is about the claim, not about the status."""
    uid = _uid("probe-unclaimed")
    await _seed_deleted(pool, uid)

    await store.set_user_avatar(uid, "data:image/png;base64,AAA")
    assert await store.set_user_preference(uid, "k", {"v": 1}) is True

    row = await pool.fetchrow(
        "SELECT avatar_data_url, preferences->'k' AS pref FROM users WHERE id=$1", uid
    )
    assert row["avatar_data_url"] == "data:image/png;base64,AAA"
    assert row["pref"] is not None
