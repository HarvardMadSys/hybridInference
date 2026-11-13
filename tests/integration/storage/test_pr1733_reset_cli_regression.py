"""The reset-password CLI against an account with a pending hard-delete claim.

The CLI is the documented operator recovery path ("This is how the
administrator created by first-run setup recovers its account ... It works for
any account"). A claimed account is being permanently erased, so its password
must not change — and the CLI must say so, in its own message and with its
own exit status, rather than letting a traceback escape.

Before the fix the store's claim guard raised ``HardDeleteStateChanged``,
which the CLI did not handle: the operator saw an unhandled traceback.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from serving.auth import reset_password as cli
from serving.storage.postgres_operational import PostgresOperationalStore
from tests.fixtures import postgres

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pool():
    pool = await postgres.create_pool(min_size=2, max_size=6)
    try:
        yield pool
    finally:
        await pool.close()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _seed_claimed(pool, uid: str) -> None:
    await pool.execute(
        "INSERT INTO users (id, email, login_name, password_hash, status) "
        "VALUES ($1, $2, $3, 'old', 'deleted')",
        uid,
        f"{uid}@example.com",
        uid,
    )
    store = PostgresOperationalStore(pool)
    await store.begin_hard_delete_user(uid)


async def test_the_cli_reports_a_claimed_account_instead_of_crashing(pool, monkeypatch):
    """The operator gets the CLI's own message, not a traceback."""
    uid = _uid("cli-claim")
    await _seed_claimed(pool, uid)

    db = postgres.target()
    monkeypatch.setenv("DB_HOST", db.host)
    monkeypatch.setenv("DB_PORT", str(db.port))
    monkeypatch.setenv("DB_NAME", db.name)
    monkeypatch.setenv("DB_USER", db.user)
    monkeypatch.setenv("DB_PASSWORD", db.password)
    monkeypatch.setenv("API_KEY_SECRET", "integration-test-secret-for-ci-only")

    with pytest.raises(cli.ResetPasswordError) as excinfo:
        await cli._reset_with_database(uid, "Sup3rSecret!Passw0rd")

    message = str(excinfo.value)
    assert "permanently deleted" in message
    assert "not changed" in message
    # The message points at the procedure that can act on it, and does not
    # imply that an ordinary Resume works: it refuses a live claim.
    assert "hard-delete recovery procedure" in message
    assert "grace period" in message
    assert "cannot be resumed at all" in message
    assert "must resume the account first" not in message

    # The password was not changed.
    row = await pool.fetchrow("SELECT password_hash FROM users WHERE id = $1", uid)
    assert row["password_hash"] == "old"


def test_the_cli_exits_with_a_useful_error_and_status_one(monkeypatch, capsys):
    """``main()`` prints the message and returns the failure status.

    Synchronous, because ``main()`` runs its own event loop, so the account
    is seeded through the CLI's own connection rather than a shared pool.
    """
    uid = _uid("cli-exit")

    db = postgres.target()
    monkeypatch.setenv("DB_HOST", db.host)
    monkeypatch.setenv("DB_PORT", str(db.port))
    monkeypatch.setenv("DB_NAME", db.name)
    monkeypatch.setenv("DB_USER", db.user)
    monkeypatch.setenv("DB_PASSWORD", db.password)
    monkeypatch.setenv("API_KEY_SECRET", "integration-test-secret-for-ci-only")

    import asyncio

    async def _seed() -> None:
        conn = await postgres.connect()
        try:
            await conn.execute(
                "INSERT INTO users (id, email, login_name, password_hash, status) "
                "VALUES ($1, $2, $3, 'old', 'deleted')",
                uid,
                f"{uid}@example.com",
                uid,
            )
        finally:
            await conn.close()
        pool = await postgres.create_pool(min_size=1, max_size=2)
        try:
            await PostgresOperationalStore(pool).begin_hard_delete_user(uid)
        finally:
            await pool.close()

    asyncio.run(_seed())

    exit_code = cli.main([uid, "--generate"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "permanently deleted" in captured.err
    assert "Traceback" not in captured.err


async def test_the_cli_still_resets_an_unclaimed_account(pool, monkeypatch):
    """The ordinary case is unchanged."""
    uid = _uid("cli-normal")
    await pool.execute(
        "INSERT INTO users (id, email, login_name, password_hash, status) "
        "VALUES ($1, $2, $3, 'old', 'active')",
        uid,
        f"{uid}@example.com",
        uid,
    )

    db = postgres.target()
    monkeypatch.setenv("DB_HOST", db.host)
    monkeypatch.setenv("DB_PORT", str(db.port))
    monkeypatch.setenv("DB_NAME", db.name)
    monkeypatch.setenv("DB_USER", db.user)
    monkeypatch.setenv("DB_PASSWORD", db.password)
    monkeypatch.setenv("API_KEY_SECRET", "integration-test-secret-for-ci-only")

    account = await cli._reset_with_database(uid, "Sup3rSecret!Passw0rd")
    assert account["id"] == uid
    row = await pool.fetchrow("SELECT password_hash FROM users WHERE id = $1", uid)
    assert row["password_hash"] != "old"
