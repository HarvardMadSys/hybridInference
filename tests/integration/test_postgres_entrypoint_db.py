"""The test-database entry point against a real PostgreSQL server."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from tests.fixtures import postgres

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
def fresh_database(monkeypatch):
    """Point the entry point at a database that does not exist yet; drop it after."""
    postgres.worker_dsn()  # skip (or fail) here if there is no server at all
    name = f"entrypoint_test_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("TEST_DB_NAME", name)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    yield name

    async def _drop() -> None:
        admin = await postgres.connect_maintenance()
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()

    asyncio.run(_drop())


async def test_first_connection_prepares_an_empty_database(fresh_database):
    """Any database test can run first: the full schema exists before it starts.

    A file that made a minimal ``api_logs`` for itself on an empty database used
    to leave every later file without ``api_logs.session_id``.
    """
    conn = await postgres.connect()
    try:
        columns = await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name IN ('api_logs', 'users')"
        )
    finally:
        await conn.close()

    assert {("api_logs", "session_id"), ("users", "signup_reason")} <= {
        (row["table_name"], row["column_name"]) for row in columns
    }


async def test_an_init_callback_error_is_an_error_not_a_skip():
    """Only an unreachable database may become a skip; an SQL bug may not."""

    async def _broken_init(conn) -> None:
        await conn.execute("SELECT no_such_column FROM pg_database")

    with pytest.raises(asyncpg.UndefinedColumnError):
        await postgres.create_pool(min_size=1, max_size=1, init=_broken_init)


def test_clean_drops_only_the_databases_of_a_removed_checkout(fresh_database, tmp_path):
    """`make test-db-local-clean`: a gone checkout's copies go, nothing else does."""
    gone, live = tmp_path / "removed-worktree", tmp_path / "live-worktree"
    gone.mkdir()
    live.mkdir()

    async def _scenario() -> tuple[list[str], set[str]]:
        await postgres.prepare_databases(1, owner=str(gone))
        await postgres.ensure_database(f"{fresh_database}_live")
        await postgres._record_owner([f"{fresh_database}_live"], str(live))
        await postgres.ensure_database(f"{fresh_database}_unmarked")
        gone.rmdir()

        # Scoped to this test's databases: other checkouts' are not ours to judge.
        dropped = await postgres.drop_orphaned_databases(prefix=fresh_database)

        admin = await postgres.connect_maintenance()
        try:
            rows = await admin.fetch(
                "SELECT datname FROM pg_database WHERE starts_with(datname, $1)", fresh_database
            )
            remaining = {row["datname"] for row in rows}
            for name in remaining:
                await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await admin.close()
        return dropped, remaining

    dropped, remaining = asyncio.run(_scenario())

    assert set(dropped) == {fresh_database, f"{fresh_database}_gw0"}
    assert remaining == {f"{fresh_database}_live", f"{fresh_database}_unmarked"}
