"""Upgrading databases that already hold something: old rows, held locks, failed and concurrent runs."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

import asyncpg
import pytest
from alembic import command

from migrations import (
    LOCK_NOT_AVAILABLE,
    MIGRATIONS_DIR,
    MigrationInProgress,
    __main__ as cli,
    alembic_config,
    sqlstate,
)
from migrations.compat import read_state
from tests.fixtures import migrations as mig, postgres, schema_snapshot

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

# The baseline's steps, as ``module.function`` under ``migrations.baseline``.
BASELINE_STEPS = (
    "app_config.ensure_schema",
    "logging_store.create_tables",
    "operational_store.create_tables",
    "responses.create_tables",
    "batch.create_tables",
    "alert_history.ensure_schema",
)


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def _describe(database: str) -> dict:
    async with mig.connected(database) as conn:
        return await schema_snapshot.describe(conn)


async def _revision(database: str) -> str | None:
    async with mig.connected(database) as conn:
        return (await read_state(conn)).revision


async def _forget_migrations(conn: asyncpg.Connection) -> None:
    """Make a migrated database look like one only startup ever built."""
    await conn.execute("DROP TABLE hybridinference_alembic_version")
    await conn.execute("DROP TABLE hybridinference_schema_compat")


async def _age(conn: asyncpg.Connection) -> None:
    """Give a baseline database the shape and the rows older releases left behind."""
    await _forget_migrations(conn)
    # Roles from before tier and role merged, under the wider CHECK of the time.
    await conn.execute("ALTER TABLE users DROP CONSTRAINT users_role_check")
    await conn.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN "
        "('trial', 'free', 'pro', 'internal', 'admin', 'internal_group', 'developer'))"
    )
    await conn.execute("ALTER TABLE users ALTER COLUMN role DROP NOT NULL")
    await conn.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    await conn.execute("ALTER TABLE users DROP CONSTRAINT users_status_check")
    await conn.execute(
        "ALTER TABLE users ADD CONSTRAINT users_status_check "
        "CHECK (status IN ('active', 'suspended', 'deleted'))"
    )
    await conn.executemany(
        "INSERT INTO users (id, email, role) VALUES ($1, $2, $3)",
        [
            ("u-null", "null@example.com", None),
            ("u-trial", "trial@example.com", "trial"),
            ("u-developer", "developer@example.com", "developer"),
            ("u-group", "group@example.com", "internal_group"),
            ("u-pro", "pro@example.com", "pro"),
        ],
    )
    # Every account had an email before first-run setup created one without.
    await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
    # Keys from before account_id, one of them for an account that is gone, and
    # the retired tier column.
    await conn.execute("ALTER TABLE api_keys ADD COLUMN tier TEXT")
    await conn.executemany(
        "INSERT INTO api_keys (key_hash, key_prefix, user_id, account_id, tier) "
        "VALUES ($1, $2, $3, NULL, 'pro')",
        [("hash-kept", "sk-kept", "u-pro"), ("hash-orphan", "sk-orphan", "u-gone")],
    )
    # Hashes very old releases stored on every request.
    await conn.execute(
        "ALTER TABLE api_logs ADD COLUMN prompt_hash TEXT, ADD COLUMN response_hash TEXT"
    )
    await conn.execute("CREATE INDEX idx_api_logs_prompt_hash ON api_logs (prompt_hash)")
    await conn.execute("CREATE INDEX idx_api_logs_response_hash ON api_logs (response_hash)")
    # A model hidden behind the trial role.
    await conn.execute(
        "INSERT INTO model_visibility_overrides (model_id, required_role) VALUES ('m-trial', 'trial')"
    )
    # Constraints from before hard delete and account deletion by phone.
    await conn.execute(
        "ALTER TABLE signup_allowed_domains DROP CONSTRAINT signup_allowed_domains_created_by_fkey, "
        "ADD CONSTRAINT signup_allowed_domains_created_by_fkey "
        "FOREIGN KEY (created_by) REFERENCES users(id)"
    )
    await conn.execute(
        "ALTER TABLE phone_auth_codes DROP CONSTRAINT phone_auth_codes_purpose_check, "
        "ADD CONSTRAINT phone_auth_codes_purpose_check CHECK (purpose IN ('signup', 'login', 'bind'))"
    )


async def test_an_old_database_converges_and_its_old_rows_are_converted(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await _age(conn)

    await mig.upgrade(scratch)

    assert schema_snapshot.differences(schema_snapshot.load(), await _describe(scratch)) == []
    async with mig.connected(scratch) as conn:
        roles = dict(await conn.fetch("SELECT id, role FROM users"))
        owners = dict(await conn.fetch("SELECT key_hash, account_id FROM api_keys"))
        override = await conn.fetchval(
            "SELECT required_role FROM model_visibility_overrides WHERE model_id = 'm-trial'"
        )
        assert (await read_state(conn)).revision == "0001_baseline"
    assert roles == {
        "u-null": "free",
        "u-trial": "free",
        "u-developer": "internal",
        "u-group": "internal",
        "u-pro": "pro",
    }
    # The account that is gone keeps its NULL: there is no owner to give it.
    assert owners == {"hash-kept": "u-pro", "hash-orphan": None}
    assert override == "free"


async def test_rows_the_migration_cannot_fix_stop_it_until_they_are_fixed(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await _forget_migrations(conn)
        await conn.execute(
            "ALTER TABLE email_broadcast_recipients "
            "DROP CONSTRAINT email_broadcast_recipients_broadcast_user_uniq, "
            "DROP CONSTRAINT email_broadcast_recipients_broadcast_id_user_id_key"
        )
        await conn.execute(
            "INSERT INTO email_broadcasts (id, subject, body_html, body_text, created_by) "
            "VALUES ('b-1', 'Hello', '<p>Hi</p>', 'Hi', 'admin')"
        )
        for _ in range(2):
            await conn.execute(
                "INSERT INTO email_broadcast_recipients (broadcast_id, user_id, email) "
                "VALUES ('b-1', 'u-1', 'u1@example.com')"
            )

    with pytest.raises(asyncpg.UniqueViolationError):
        await mig.upgrade(scratch)
    assert await _revision(scratch) is None

    async with mig.connected(scratch) as conn:
        await conn.execute(
            "DELETE FROM email_broadcast_recipients WHERE ctid NOT IN "
            "(SELECT min(ctid) FROM email_broadcast_recipients GROUP BY broadcast_id, user_id)"
        )
    await mig.upgrade(scratch)
    assert await _revision(scratch) == "0001_baseline"


@pytest.mark.parametrize("step", BASELINE_STEPS)
async def test_a_run_that_fails_partway_is_finished_by_the_next(scratch, step, monkeypatch):
    module_name, function = step.split(".")
    module = importlib.import_module(f"migrations.baseline.{module_name}")

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"injected failure in {step}")

    with monkeypatch.context() as patch:
        patch.setattr(module, function, fail)
        with pytest.raises(RuntimeError, match="injected failure"):
            await mig.upgrade(scratch)
    assert await _revision(scratch) is None

    await mig.upgrade(scratch)

    assert schema_snapshot.differences(schema_snapshot.load(), await _describe(scratch)) == []
    assert await _revision(scratch) == "0001_baseline"


async def _hold_api_keys(database: str) -> tuple[asyncpg.Connection, object]:
    """Hold ``ACCESS SHARE`` on ``api_keys`` in an open transaction, as ``pg_dump`` does."""
    holder = await postgres.connect(database=database)
    transaction = holder.transaction()
    await transaction.start()
    await holder.execute("LOCK TABLE api_keys IN ACCESS SHARE MODE")
    return holder, transaction


async def test_a_held_table_lock_stops_the_run_and_the_rerun_finishes(scratch):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await _forget_migrations(conn)
        # Dropping it needs ACCESS EXCLUSIVE, which the reader's lock blocks.
        await conn.execute("ALTER TABLE api_keys ADD COLUMN tier TEXT")
    holder, transaction = await _hold_api_keys(scratch)
    try:
        with pytest.raises(Exception) as caught:
            await mig.upgrade(scratch)
        assert sqlstate(caught.value) == LOCK_NOT_AVAILABLE
        assert await _revision(scratch) is None
    finally:
        await transaction.rollback()
        await holder.close()

    await mig.upgrade(scratch)

    assert schema_snapshot.differences(schema_snapshot.load(), await _describe(scratch)) == []


async def test_the_command_exits_75_and_names_the_session_holding_the_lock(
    scratch, monkeypatch, capsys
):
    await mig.upgrade(scratch)
    async with mig.connected(scratch) as conn:
        await _forget_migrations(conn)
        await conn.execute("ALTER TABLE api_keys ADD COLUMN tier TEXT")
    for name, value in postgres.target().app_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DB_NAME", scratch)
    holder, transaction = await _hold_api_keys(scratch)
    try:
        await holder.execute("SET application_name = 'pg_dump'")
        exit_code = await asyncio.to_thread(cli.upgrade, "head")
        holder_pid = holder.get_server_pid()
    finally:
        await transaction.rollback()
        await holder.close()

    assert exit_code == cli.EXIT_RETRY
    err = capsys.readouterr().err
    assert f"pid {holder_pid} pg_dump" in err
    assert "api_keys (AccessShareLock)" in err


def _command_env(database: str) -> dict[str, str]:
    """The environment ``python -m migrations`` reads, pointed at *database*."""
    env = {**os.environ, **postgres.target().app_env(), "DB_NAME": database}
    env["PYTHONPATH"] = os.pathsep.join([str(MIGRATIONS_DIR.parent), env.get("PYTHONPATH", "")])
    return env


async def _run_command(database: str, *args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "migrations",
        *args,
        env=_command_env(database),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode()


async def test_concurrent_runs_take_turns(scratch):
    """Two deploys migrating at once: one waits for the other, neither fails."""
    results = await asyncio.gather(
        _run_command(scratch, "upgrade"), _run_command(scratch, "upgrade")
    )

    assert [code for code, _ in results] == [0, 0], results

    assert schema_snapshot.differences(schema_snapshot.load(), await _describe(scratch)) == []
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval("SELECT count(*) FROM hybridinference_alembic_version") == 1


async def test_a_run_that_cannot_get_the_run_lock_changes_nothing(scratch):
    holder = await postgres.connect(database=scratch)
    try:
        await holder.execute(
            "SELECT pg_advisory_lock(hashtext('hybridinference:schema-migrations'))"
        )
        config = alembic_config(mig.url(scratch))
        config.attributes["run_lock_wait"] = "1s"
        with pytest.raises(MigrationInProgress):
            await asyncio.to_thread(command.upgrade, config, "head")
    finally:
        await holder.close()

    async with mig.connected(scratch) as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == 0
        )
