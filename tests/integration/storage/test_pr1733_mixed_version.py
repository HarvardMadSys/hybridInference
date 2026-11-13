"""Mixed-version verification for PR #1733.

Two directions matter when a revision adds a column.

**Old code, new schema** (the supported direction). The revision
``0016_hard_delete_claim_lease`` is expand-only, so the previous release's
code keeps working on a database that has been migrated to 0016: it never
selects the new columns and never writes them. This test drives the previous
release's own hard-delete statements against a 0016 database.

**New code, old schema** (the unsupported direction). The claim path reads
``hard_delete_claimed_at``, which exists only from 0016, so on a 0015
database it raises ``asyncpg.UndefinedColumnError``. That combination is not
reachable in a deployment, and this test records why:

- the gateway refuses to start on a database behind its own chain
  (``migrations.compat.require_schema`` raises ``SchemaNotReady``), so the
  server never runs 0016's claim path on a 0015 database;
- the only caller of the claim path is the admin route, in the same process;
- the reset-password CLI, which documents that it "also works on a database
  the schema migrations have not brought up to date", writes through
  ``update_user_fields``, which reads only ``hard_delete_pending`` — a
  baseline column — so the CLI keeps working on a 0015 database.

The supported direction is the one the boot guard leaves open: a database
already at 0016 with the previous release still serving.
"""

from __future__ import annotations

import pytest

from migrations.compat import SchemaState, Verdict, code_chain, judge
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


async def test_the_reset_password_cli_works_on_a_pre_0016_database(scratch):
    """The CLI's documented contract survives the PR's new column read."""
    # A database the migrations have not brought up to date: 0015 is the
    # revision immediately before the PR's.
    await mig.upgrade(scratch, "0015_projects")

    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, login_name, password_hash, status) "
            "VALUES ('cli-u', 'cli@test.test', 'cliuser', 'old-hash', 'active')"
        )

    from serving.auth.reset_password import AccountStore, reset_password
    from tests.fixtures import postgres

    pool = await postgres.create_pool(min_size=1, max_size=2, database=scratch)
    try:
        # The CLI's own wiring: a pool over the deployment's database.
        account_store = AccountStore(pool)
        account = await reset_password(account_store, "cliuser", "Sup3rSecret!Passw0rd")
        assert account["id"] == "cli-u"
    finally:
        await pool.close()

    async with mig.connected(scratch) as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE id = 'cli-u'")
    assert row["password_hash"] != "old-hash"


async def test_the_lease_columns_exist_only_from_0016(scratch):
    """The premise: 0015 has no lease columns."""
    await mig.upgrade(scratch, "0015_projects")
    async with mig.connected(scratch) as conn:
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='users' AND column_name LIKE 'hard_delete%'"
            )
        }
    assert "hard_delete_pending" in cols
    assert "hard_delete_claimed_at" not in cols
    assert "hard_delete_claim_recovered" not in cols


async def test_the_claim_path_requires_the_lease_columns(scratch):
    """The store's requirement: the claim path needs 0016's columns."""
    await mig.upgrade(scratch, "0015_projects")
    async with mig.connected(scratch) as conn:
        await conn.execute(
            "INSERT INTO users (id, email, status) VALUES ('mix-u', 'mix@test.test', 'deleted')"
        )

    from serving.storage.postgres_operational import PostgresOperationalStore
    from tests.fixtures import postgres

    pool = await postgres.create_pool(min_size=1, max_size=4, database=scratch)
    try:
        store = PostgresOperationalStore(pool)
        with pytest.raises(Exception) as excinfo:
            await store.begin_hard_delete_user("mix-u")
        assert "hard_delete_claimed_at" in str(excinfo.value)
    finally:
        await pool.close()


async def test_the_boot_guard_refuses_a_database_behind_this_code():
    """The reason the combination above is unreachable in a deployment."""
    chain = code_chain()
    assert judge(SchemaState(revision="0015_projects", floor=None), chain) is Verdict.BEHIND
    assert (
        judge(SchemaState(revision="0016_hard_delete_claim_lease", floor=None), chain)
        is Verdict.CURRENT
    )


async def test_the_previous_releases_hard_delete_statements_run_on_a_0016_database(scratch):
    """Old code, new schema: the supported mixed-version direction."""
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    async with mig.connected(scratch) as conn:
        # The statements the previous release's hard-delete path used.
        await conn.execute(
            "INSERT INTO users (id, email, status, hard_delete_pending, hard_delete_claim_token) "
            "VALUES ('old-u', 'old@test.test', 'deleted', TRUE, 'old-token')"
        )
        row = await conn.fetchrow(
            "SELECT status, hard_delete_pending, hard_delete_claim_token FROM users "
            "WHERE id = 'old-u'"
        )
        assert row["hard_delete_pending"] is True
        assert row["hard_delete_claim_token"] == "old-token"
        # The old release's release-statement still works.
        await conn.execute(
            "UPDATE users SET hard_delete_pending = FALSE, hard_delete_claim_token = NULL "
            "WHERE id = 'old-u' AND status = 'deleted' AND hard_delete_pending = TRUE "
            "AND hard_delete_claim_token = 'old-token'"
        )
        after = await conn.fetchrow("SELECT hard_delete_pending FROM users WHERE id = 'old-u'")
        assert after["hard_delete_pending"] is False
