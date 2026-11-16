"""``0019_two_factor`` and the statements that keep a second factor (``serving.storage.two_factor``).

Against a scratch database built by the migrations: what the revision adds,
that the previous release's session statements still run on it, and the
places where a comparison and a write must be one statement or one
transaction: a code is accepted once however many requests present it at the
same moment, a setup gets only the tries it has, and a code accepted for one
authenticator never verifies a session after another has replaced it.
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


SETUP = {"ttl_seconds": 600, "attempts": 5}
CHROME = "chrome/macos"


async def _enroll(store, user_id: str, *, step: int = 100, codes=("c1", "c2")) -> None:
    """Give *user_id* the authenticator ``pending-<user_id>``, set up from session ``setup``."""
    await store.set_pending_two_factor(user_id, f"pending-{user_id}", sid="setup", **SETUP)
    assert await store.confirm_two_factor(
        user_id,
        pending_secret_encrypted=f"pending-{user_id}",
        step=step,
        recovery_code_hashes=list(codes),
        sid="setup",
        environment=CHROME,
    )


async def _code(store, user_id: str, sid: str, step: int, *, secret: str | None = None) -> str:
    """Present a code from the app for *step*, checked against *secret*."""
    return await store.verify_session_two_factor(
        user_id, sid, CHROME, secret_encrypted=secret or f"pending-{user_id}", step=step
    )


async def _recovery(store, user_id: str, sid: str, code_hash: str) -> str:
    return await store.verify_session_two_factor(user_id, sid, CHROME, recovery_code_hash=code_hash)


async def _verified(store, user_id: str, sid: str) -> bool:
    return (await store.get_session_two_factor(user_id, sid))["verified_at"] is not None


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
    await _session(store, "u-ann", "browser")

    await store.set_pending_two_factor("u-ann", "enc-first", sid="browser", **SETUP)
    await store.set_pending_two_factor("u-ann", "enc-second", sid="browser", **SETUP)  # again
    row = await store.get_two_factor("u-ann")
    assert (row["secret_encrypted"], row["pending_session_id"]) == (None, "browser")
    assert row["confirmed_at"] is None
    # Nothing a pending secret or its codes can be used for yet.
    assert await _code(store, "u-ann", "browser", 5, secret="enc-second") == "rejected"
    assert not await store.replace_recovery_codes("u-ann", ["x"])
    assert (await store.get_session_two_factor("u-ann", "browser"))["enrolled"] is False

    # The secret a request was shown is the one it confirms, or nothing is.
    confirm = {"step": 7, "sid": "browser", "environment": CHROME}
    assert not await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-first", recovery_code_hashes=["a"], **confirm
    )
    assert await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-second", recovery_code_hashes=["a", "b"], **confirm
    )
    row = await store.get_two_factor("u-ann")
    assert (row["secret_encrypted"], row["pending_session_id"]) == ("enc-second", None)
    assert row["confirmed_at"] is not None
    assert (row["last_used_step"], row["recovery_codes_left"]) == (7, 2)
    # The session that gave the first code is verified by it.
    assert await _verified(store, "u-ann", "browser")
    # And only once.
    assert not await store.confirm_two_factor(
        "u-ann", pending_secret_encrypted="enc-second", recovery_code_hashes=["z"], **confirm
    )


async def test_a_setup_gets_the_tries_it_has_however_many_arrive_at_once(store):
    """Confirming hands over the second factor, so a setup is not a place to guess."""
    await store.set_pending_two_factor("u-ann", "enc", sid="browser", **SETUP)

    results = await asyncio.gather(
        *(store.begin_two_factor_setup_attempt("u-ann", "browser") for _ in range(40))
    )

    assert (results.count("enc"), results.count(None)) == (5, 35)
    # Out of tries: nothing waits any more, for a right code either.
    assert (await store.get_two_factor("u-ann"))["pending_session_id"] is None
    assert await store.begin_two_factor_setup_attempt("u-ann", "browser") is None
    # Starting again, which the routes charge the password or a code for, buys five more.
    await store.set_pending_two_factor("u-ann", "enc-2", sid="browser", **SETUP)
    assert await store.begin_two_factor_setup_attempt("u-ann", "browser") == "enc-2"


async def test_a_setup_belongs_to_the_session_that_started_it_and_runs_out(store):
    await store.set_pending_two_factor("u-ann", "enc", sid="browser", **SETUP)

    # Another session of the account, and another account, have nothing to confirm.
    assert await store.begin_two_factor_setup_attempt("u-ann", "laptop") is None
    assert await store.begin_two_factor_setup_attempt("u-bob", "browser") is None
    assert not await store.cancel_pending_two_factor("u-ann", "laptop")
    # ... and took none of its tries.
    async with store._pool.acquire() as conn:
        assert await conn.fetchval("SELECT pending_attempts_left FROM user_two_factor") == 5
        await conn.execute(
            "UPDATE user_two_factor SET pending_expires_at = NOW() - INTERVAL '1 second'"
        )

    # Left waiting too long, it is gone for its own session too.
    assert await store.begin_two_factor_setup_attempt("u-ann", "browser") is None
    assert (await store.get_two_factor("u-ann"))["pending_session_id"] is None

    await store.set_pending_two_factor("u-ann", "enc-2", sid="browser", **SETUP)
    assert await store.cancel_pending_two_factor("u-ann", "browser")
    assert await store.begin_two_factor_setup_attempt("u-ann", "browser") is None
    assert not await store.cancel_pending_two_factor("u-ann", "browser")


async def test_cancelling_a_replacement_keeps_the_authenticator(store):
    await _enroll(store, "u-ann")
    await store.set_pending_two_factor("u-ann", "enc-new", sid="browser", **SETUP)

    assert await store.cancel_pending_two_factor("u-ann", "browser")

    row = await store.get_two_factor("u-ann")
    assert (row["secret_encrypted"], row["pending_session_id"]) == ("pending-u-ann", None)
    assert row["recovery_codes_left"] == 2


async def test_a_time_step_is_accepted_once_even_when_presented_at_once(store):
    await _enroll(store, "u-ann", step=100)
    await _session(store, "u-ann", "browser")

    assert await _code(store, "u-ann", "browser", 100) == "rejected"
    assert await _code(store, "u-ann", "browser", 99) == "rejected"
    assert not await _verified(store, "u-ann", "browser")
    results = await asyncio.gather(*(_code(store, "u-ann", "browser", 101) for _ in range(8)))
    assert sorted(results) == ["rejected"] * 7 + ["verified"]
    assert (await store.get_two_factor("u-ann"))["last_used_step"] == 101
    assert await _verified(store, "u-ann", "browser")
    # An account without an authenticator accepts none.
    await _session(store, "u-bob", "browser")
    assert await _code(store, "u-bob", "browser", 101) == "rejected"


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
    await _session(store, "u-ann", "browser")
    assert await _code(store, "u-ann", "browser", 101) == "verified"
    assert await _attempts(store, "u-ann") == (0, None)
    for _ in range(5):
        await store.begin_two_factor_attempt("u-ann", **LOCK)
    assert (await _attempts(store, "u-ann"))[1] is not None
    assert await _recovery(store, "u-ann", "browser", "c1") == "verified"
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
    await _session(store, "u-ann", "browser")

    assert await _recovery(store, "u-ann", "browser", "b1") == "rejected"
    assert await _recovery(store, "u-ann", "browser", "unknown") == "rejected"
    results = await asyncio.gather(*(_recovery(store, "u-ann", "browser", "c1") for _ in range(8)))
    assert sorted(results) == ["rejected"] * 7 + ["verified"]
    assert (await store.get_two_factor("u-ann"))["recovery_codes_left"] == 2

    assert await store.replace_recovery_codes("u-ann", ["n1", "n2"])
    assert await _recovery(store, "u-ann", "browser", "c2") == "rejected"
    assert await _recovery(store, "u-ann", "browser", "n1") == "verified"
    assert (await store.get_two_factor("u-bob"))["recovery_codes_left"] == 1


async def test_a_good_code_for_a_session_that_is_gone_is_not_spent(store):
    await _enroll(store, "u-ann", step=100, codes=("c1",))
    await _session(store, "u-ann", "browser")

    assert await _code(store, "u-ann", "no-such-session", 101) == "session_ended"
    assert await _recovery(store, "u-ann", "no-such-session", "c1") == "session_ended"
    # Another account's session id is no session of this one.
    await _session(store, "u-bob", "laptop")
    assert await _code(store, "u-ann", "laptop", 101) == "session_ended"

    row = await store.get_two_factor("u-ann")
    assert (row["last_used_step"], row["recovery_codes_left"]) == (100, 1)
    assert await _code(store, "u-ann", "browser", 101) == "verified"
    assert await _recovery(store, "u-ann", "browser", "c1") == "verified"


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

    assert await _code(store, "u-ann", "browser", 101) == "verified"
    browser = await store.get_session_two_factor("u-ann", "browser")
    assert browser["environment"] == CHROME
    assert datetime.now(UTC) - browser["verified_at"] < timedelta(minutes=1)
    assert not await _verified(store, "u-ann", "laptop")

    assert await store.revoke_session_by_sid("u-ann", "browser")
    assert await store.get_session_two_factor("u-ann", "browser") is None
    assert await _code(store, "u-ann", "browser", 102) == "session_ended"
    await _session(store, "u-ann", "expired", days=-1)
    assert await store.get_session_two_factor("u-ann", "expired") is None


async def test_a_new_device_or_none_unverifies_every_session(store):
    await _session(store, "u-ann", "browser")
    await _session(store, "u-ann", "laptop")
    await _session(store, "u-bob", "browser")
    await _enroll(store, "u-ann")
    await _enroll(store, "u-bob")
    assert await _code(store, "u-ann", "browser", 101) == "verified"
    assert await _code(store, "u-bob", "browser", 101) == "verified"

    # A replacement keeps the old device working until it is confirmed ...
    await store.set_pending_two_factor("u-ann", "enc-new", sid="laptop", **SETUP)
    assert (await store.get_two_factor("u-ann"))["secret_encrypted"] == "pending-u-ann"
    assert await _verified(store, "u-ann", "browser")
    # ... and then what sessions proved with the old one is gone, and the
    # session that gave the new one's first code is the one left verified.
    assert await store.confirm_two_factor(
        "u-ann",
        pending_secret_encrypted="enc-new",
        step=1,
        recovery_code_hashes=["n"],
        sid="laptop",
        environment="firefox/linux",
    )
    assert (await store.get_two_factor("u-ann"))["secret_encrypted"] == "enc-new"
    assert not await _verified(store, "u-ann", "browser")
    laptop = await store.get_session_two_factor("u-ann", "laptop")
    assert (laptop["verified_at"] is not None, laptop["environment"]) == (True, "firefox/linux")
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


async def test_the_old_devices_codes_verify_nothing_once_it_is_replaced(store):
    """What a request checked against the old secret says nothing about the new one."""
    await _enroll(store, "u-ann", step=100, codes=("old-1", "old-2"))
    await _session(store, "u-ann", "browser")
    await store.set_pending_two_factor("u-ann", "enc-new", sid="setup", **SETUP)
    assert await store.confirm_two_factor(
        "u-ann",
        pending_secret_encrypted="enc-new",
        step=1,
        recovery_code_hashes=["new-1"],
        sid="setup",
        environment=CHROME,
    )

    # A step far newer than the new device's last one, but for the old secret.
    assert await _code(store, "u-ann", "browser", 500) == "rejected"
    assert await _recovery(store, "u-ann", "browser", "old-1") == "rejected"
    assert not await _verified(store, "u-ann", "browser")
    assert (await store.get_two_factor("u-ann"))["last_used_step"] == 1
    assert await _code(store, "u-ann", "browser", 500, secret="enc-new") == "verified"


@pytest.mark.parametrize("credential", ["code", "recovery_code"])
@pytest.mark.parametrize("change", ["replaced", "removed"])
async def test_a_code_given_while_the_authenticator_changes_cannot_outlive_it(
    store, credential, change
):
    """The race a split check-then-mark lost: old code accepted, device changed, session marked.

    A change of authenticator holds the account's row and has not committed.
    A request presenting the old device's credential arrives. It must wait for
    the change, and then find the credential worthless, rather than mark its
    session verified after the change has forgotten what sessions proved.
    """
    await _enroll(store, "u-ann", step=100, codes=("old-1",))
    await _session(store, "u-ann", "browser")
    await store.set_pending_two_factor("u-ann", "enc-new", sid="setup", **SETUP)

    async with store._pool.acquire() as conn:
        changing = conn.transaction()
        await changing.start()
        if change == "replaced":
            await conn.execute(
                "UPDATE user_two_factor SET secret_encrypted = pending_secret_encrypted, "
                "pending_secret_encrypted = NULL, last_used_step = 1 WHERE user_id = 'u-ann'"
            )
            await conn.execute("DELETE FROM two_factor_recovery_codes WHERE user_id = 'u-ann'")
        else:
            await conn.execute("DELETE FROM user_two_factor WHERE user_id = 'u-ann'")
        await conn.execute(
            "UPDATE auth_sessions SET two_factor_verified_at = NULL WHERE user_id = 'u-ann'"
        )

        presented = asyncio.create_task(
            _code(store, "u-ann", "browser", 500)
            if credential == "code"
            else _recovery(store, "u-ann", "browser", "old-1")
        )
        # It is waiting on the row, not finished: the change is not visible yet.
        done, _pending = await asyncio.wait({presented}, timeout=0.5)
        assert not done
        await changing.commit()

    assert await presented == "rejected"
    assert not await _verified(store, "u-ann", "browser")


async def test_deleting_the_account_removes_its_second_factor(store):
    await _enroll(store, "u-ann", codes=("c1", "c2"))

    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = 'u-ann'")
        assert await conn.fetchval("SELECT COUNT(*) FROM user_two_factor") == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM two_factor_recovery_codes") == 0
