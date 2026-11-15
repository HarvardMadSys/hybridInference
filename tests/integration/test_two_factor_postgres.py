"""``0019_two_factor`` and the statements that keep a second factor (``serving.storage.two_factor``).

Against a scratch database built by the migrations: what the revision adds,
that the previous release's session statements still run on it, and the two
places where one statement must both compare and write so a code is accepted
once however many requests present it at the same moment.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import migrations as mig, postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


@pytest.fixture
async def store(scratch):
    await mig.upgrade(scratch, "0019_two_factor")
    pool = await postgres.create_pool(database=scratch, min_size=1, max_size=8)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, password_hash, user_name) VALUES "
                "('u-ann', 'ann@x.test', 'h', 'ann'), ('u-bob', 'bob@x.test', 'h', 'bob')"
            )
        yield PostgresOperationalStore(pool)
    finally:
        await pool.close()


async def _session(store, user_id: str, sid: str, *, days: int = 1) -> None:
    await store.create_session(
        session_id=f"{user_id}-{sid}",
        user_id=user_id,
        refresh_token_hash=uuid.uuid4().hex,
        jti=uuid.uuid4().hex,
        sid=sid,
        expires_at=datetime.now(UTC) + timedelta(days=days),
    )


async def _enroll(store, user_id: str, *, step: int = 100, codes=("c1", "c2")) -> None:
    await store.set_pending_two_factor(user_id, f"pending-{user_id}")
    assert await store.confirm_two_factor(
        user_id,
        pending_secret_encrypted=f"pending-{user_id}",
        step=step,
        recovery_code_hashes=list(codes),
    )


async def test_the_revision_adds_two_tables_and_two_nullable_session_columns(scratch):
    await mig.upgrade(scratch, "0018_projects_owned_by_accounts")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO auth_sessions (id, user_id, refresh_token_hash, sid, expires_at) "
            "VALUES ('s-old', 'u-1', 'h-old', 'sid-old', NOW() + INTERVAL '1 day')"
        )

    await mig.upgrade(scratch, "0019_two_factor")

    async with mig.connected(scratch) as conn:
        tables = {
            row["table_name"]
            for row in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('user_two_factor', 'two_factor_recovery_codes')"
            )
        }
        assert tables == {"user_two_factor", "two_factor_recovery_codes"}
        columns = {
            row["column_name"]: row["is_nullable"]
            for row in await conn.fetch(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'auth_sessions' AND column_name LIKE 'two_factor_%'"
            )
        }
        assert columns == {"two_factor_verified_at": "YES", "two_factor_environment": "YES"}
        # A session from before has proved nothing.
        old = await conn.fetchrow(
            "SELECT two_factor_verified_at, two_factor_environment FROM auth_sessions "
            "WHERE id = 's-old'"
        )
        assert tuple(old) == (None, None)
        # The previous release's insert names its columns and still runs.
        await conn.execute(
            "INSERT INTO auth_sessions "
            "(id, user_id, refresh_token_hash, jti, sid, expires_at, revoked) "
            "VALUES ('s-new', 'u-1', 'h-new', 'j', 'sid-new', NOW() + INTERVAL '1 day', FALSE)"
        )

    # Running it again changes nothing.
    await mig.upgrade(scratch, "0019_two_factor")


async def test_a_secret_counts_only_once_its_first_code_is_given(store):
    assert await store.get_two_factor("u-ann") is None

    await store.set_pending_two_factor("u-ann", "enc-first")
    await store.set_pending_two_factor("u-ann", "enc-second")  # setup started again
    row = await store.get_two_factor("u-ann")
    assert (row["secret_encrypted"], row["pending_secret_encrypted"]) == (None, "enc-second")
    assert row["confirmed_at"] is None
    # Nothing a pending secret or its codes can be used for yet.
    assert not await store.use_two_factor_step("u-ann", 5)
    assert not await store.replace_recovery_codes("u-ann", ["x"])

    # The secret a request was shown is the one it confirms, or nothing is.
    assert not await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-first", step=7, recovery_code_hashes=["a"]
    )
    assert await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-second", step=7, recovery_code_hashes=["a", "b"]
    )
    row = await store.get_two_factor("u-ann")
    assert (row["secret_encrypted"], row["pending_secret_encrypted"]) == ("enc-second", None)
    assert row["confirmed_at"] is not None
    assert (row["last_used_step"], row["recovery_codes_left"]) == (7, 2)
    # And only once.
    assert not await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-second", step=8, recovery_code_hashes=["z"]
    )


async def test_a_time_step_is_accepted_once_even_when_presented_at_once(store):
    await _enroll(store, "u-ann", step=100)

    assert not await store.use_two_factor_step("u-ann", 100)
    assert not await store.use_two_factor_step("u-ann", 99)
    results = await asyncio.gather(*(store.use_two_factor_step("u-ann", 101) for _ in range(8)))
    assert sorted(results) == [False] * 7 + [True]
    assert (await store.get_two_factor("u-ann"))["last_used_step"] == 101
    # An account without an authenticator accepts none.
    assert not await store.use_two_factor_step("u-bob", 101)


LOCK = {"lock_every": 5, "lock_seconds": 900, "max_lock_seconds": 86400}


async def _attempts(store, user_id: str) -> tuple[int, datetime | None]:
    row = await store.get_two_factor(user_id)
    return row["failed_attempts"], row["locked_until"]


async def test_guesses_sent_together_are_let_through_one_run_at_a_time(store):
    """However many requests race, five are checked and the rest are held off."""
    await _enroll(store, "u-ann")

    results = await asyncio.gather(
        *(store.begin_two_factor_attempt("u-ann", **LOCK) for _ in range(40))
    )

    assert sum(1 for held_until in results if held_until is None) == 5
    count, locked_until = await _attempts(store, "u-ann")
    assert count == 5
    hold = (locked_until - datetime.now(UTC)).total_seconds()
    assert 880 < hold <= 900
    # Every attempt that was held off was told when the hold ends.
    assert {held_until for held_until in results if held_until is not None} == {locked_until}
    # An account without an authenticator has nothing to attempt.
    assert await store.begin_two_factor_attempt("u-bob", **LOCK) is not None


async def test_the_hold_doubles_to_a_ceiling_and_a_right_code_ends_it(store):
    await _enroll(store, "u-ann", step=100)
    holds = []

    for _run in range(9):
        for _ in range(5):
            assert await store.begin_two_factor_attempt("u-ann", **LOCK) is None
        _count, locked_until = await _attempts(store, "u-ann")
        holds.append(round((locked_until - datetime.now(UTC)).total_seconds() / 900))
        assert await store.begin_two_factor_attempt("u-ann", **LOCK) == locked_until
        async with store._pool.acquire() as conn:  # the hold runs out
            await conn.execute(
                "UPDATE user_two_factor SET locked_until = NOW() - INTERVAL '1 second'"
            )

    assert holds == [1, 2, 4, 8, 16, 32, 64, 96, 96]  # 96 quarter-hours: a day
    # Between holds the count is kept, so the next run is held for longer still ...
    assert await store.begin_two_factor_attempt("u-ann", **LOCK) is None
    assert await _attempts(store, "u-ann") == (46, None)
    # ... until a code is accepted, from the app or from the recovery sheet.
    assert await store.use_two_factor_step("u-ann", 101)
    assert await _attempts(store, "u-ann") == (0, None)
    for _ in range(5):
        await store.begin_two_factor_attempt("u-ann", **LOCK)
    assert (await _attempts(store, "u-ann"))[1] is not None
    assert await store.use_recovery_code("u-ann", "c1")
    assert await _attempts(store, "u-ann") == (0, None)


async def test_a_far_longer_run_than_any_real_one_does_not_overflow(store):
    await _enroll(store, "u-ann")
    async with store._pool.acquire() as conn:
        await conn.execute("UPDATE user_two_factor SET failed_attempts = 2000000004")

    assert await store.begin_two_factor_attempt("u-ann", **LOCK) is None

    _count, locked_until = await _attempts(store, "u-ann")
    assert 86000 < (locked_until - datetime.now(UTC)).total_seconds() <= 86400


async def test_a_recovery_code_is_spent_once_and_belongs_to_its_account(store):
    await _enroll(store, "u-ann", codes=("c1", "c2", "c3"))
    await _enroll(store, "u-bob", codes=("b1",))

    assert not await store.use_recovery_code("u-ann", "b1")
    assert not await store.use_recovery_code("u-ann", "unknown")
    results = await asyncio.gather(*(store.use_recovery_code("u-ann", "c1") for _ in range(8)))
    assert sorted(results) == [False] * 7 + [True]
    assert (await store.get_two_factor("u-ann"))["recovery_codes_left"] == 2

    assert await store.replace_recovery_codes("u-ann", ["n1", "n2"])
    assert not await store.use_recovery_code("u-ann", "c2")
    assert await store.use_recovery_code("u-ann", "n1")
    assert (await store.get_two_factor("u-bob"))["recovery_codes_left"] == 1


async def test_what_a_session_proved_is_its_own_and_ends_with_the_session(store):
    await _enroll(store, "u-ann")
    await _session(store, "u-ann", "browser")
    await _session(store, "u-ann", "laptop")
    await _session(store, "u-bob", "browser")

    assert await store.get_session_two_factor("u-ann", "browser") == {
        "verified_at": None,
        "environment": None,
        "enrolled": True,
    }
    assert (await store.get_session_two_factor("u-bob", "browser"))["enrolled"] is False
    assert await store.get_session_two_factor("u-ann", "no-such-session") is None
    # Another account's session id proves nothing for this one.
    assert not await store.mark_session_two_factor("u-bob", "laptop", "chrome/macos")

    assert await store.mark_session_two_factor("u-ann", "browser", "chrome/macos")
    browser = await store.get_session_two_factor("u-ann", "browser")
    assert browser["environment"] == "chrome/macos"
    assert datetime.now(UTC) - browser["verified_at"] < timedelta(minutes=1)
    assert (await store.get_session_two_factor("u-ann", "laptop"))["verified_at"] is None

    assert await store.revoke_session_by_sid("u-ann", "browser")
    assert await store.get_session_two_factor("u-ann", "browser") is None
    assert not await store.mark_session_two_factor("u-ann", "browser", "chrome/macos")
    await _session(store, "u-ann", "expired", days=-1)
    assert await store.get_session_two_factor("u-ann", "expired") is None


async def test_a_new_device_or_none_unverifies_every_session(store):
    await _enroll(store, "u-ann")
    await _session(store, "u-ann", "browser")
    await _session(store, "u-bob", "browser")
    await _enroll(store, "u-bob")
    await store.mark_session_two_factor("u-ann", "browser", "chrome/macos")
    await store.mark_session_two_factor("u-bob", "browser", "firefox/linux")

    # A replacement keeps the old device working until it is confirmed ...
    await store.set_pending_two_factor("u-ann", "enc-new")
    assert (await store.get_two_factor("u-ann"))["secret_encrypted"] == "pending-u-ann"
    assert (await store.get_session_two_factor("u-ann", "browser"))["verified_at"] is not None
    # ... and then what sessions proved with the old one is gone.
    assert await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-new", step=1, recovery_code_hashes=["n"]
    )
    assert (await store.get_two_factor("u-ann"))["secret_encrypted"] == "enc-new"
    assert (await store.get_session_two_factor("u-ann", "browser"))["verified_at"] is None
    assert (await store.get_two_factor("u-ann"))["recovery_codes_left"] == 1

    assert await store.delete_two_factor("u-bob")
    assert await store.get_two_factor("u-bob") is None
    assert await store.get_session_two_factor("u-bob", "browser") == {
        "verified_at": None,
        "environment": None,
        "enrolled": False,
    }
    assert not await store.delete_two_factor("u-bob")
    async with store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM two_factor_recovery_codes") == 1


async def test_deleting_the_account_removes_its_second_factor(store):
    await _enroll(store, "u-ann", codes=("c1", "c2"))

    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = 'u-ann'")
        assert await conn.fetchval("SELECT COUNT(*) FROM user_two_factor") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM two_factor_recovery_codes") == 0
