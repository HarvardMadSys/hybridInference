"""Migration-chain verification for PR #1733 (invariant 9 and mixed-version).

The PR adds ``0016_hard_delete_claim_lease`` and a mergepoint joining it with
current ``dev``. These tests check the graph forms exactly one head, that the
claim revision is an *expand* (earlier code keeps working on its result),
that upgrade/downgrade/re-upgrade behave, and that older code remains
compatible with the expanded schema.
"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command

from migrations import MIGRATIONS_DIR, alembic_config
from migrations.compat import SchemaState, Verdict, code_chain, judge
from tests.fixtures import migrations as mig

pytestmark = [pytest.mark.integration, pytest.mark.dbtest]

VERSIONS = MIGRATIONS_DIR / "versions"


@pytest.fixture
async def scratch():
    async with mig.scratch_database() as name:
        yield name


def test_the_chain_has_one_head_and_includes_the_claim_mergepoint():
    chain = code_chain()
    assert len(chain) == len(set(chain)), "a revision appears twice"
    # Later additive revisions may follow the claim mergepoint (for example,
    # #1781's alert preload indexes), so the mergepoint need not be the tip.
    assert "0022_hard_delete_merge" in chain
    assert "0016_hard_delete_claim_lease" in chain
    # Every revision file is in the graph, which has one merged head.
    files = sorted(p.stem for p in VERSIONS.glob("*.py") if p.stem != "__init__")
    assert len(files) == len(chain)


def test_the_new_revision_is_expand_so_earlier_code_still_works():
    """Earlier code must keep working on the result of 0016."""
    source = (VERSIONS / "0016_hard_delete_claim_lease.py").read_text()
    assert 'compatibility = "expand"' in source
    # The revision must not remove, rename or retype anything.
    lowered = source.lower()
    for forbidden in (
        "drop column",
        "drop table",
        "rename",
        "alter column",
        "set not null",
        "create unique index",
    ):
        assert forbidden not in lowered, forbidden


def test_a_database_at_the_previous_revision_is_refused_by_this_code():
    """A database at 0015 is behind this code, so this code must not start on it."""
    verdict = judge(SchemaState(revision="0015_projects", floor=None), code_chain())
    assert verdict is not Verdict.CURRENT


def test_a_database_at_the_mergepoint_matches_the_current_chain_position():
    chain = code_chain()
    verdict = judge(SchemaState(revision="0022_hard_delete_merge", floor=None), chain)
    expected = Verdict.CURRENT if chain[-1] == "0022_hard_delete_merge" else Verdict.BEHIND
    assert verdict is expected


def test_a_newer_database_is_accepted_as_compatible():
    """A database ahead of this code is fine: 0016 only added columns."""
    verdict = judge(SchemaState(revision="0017_something_later", floor=None), code_chain())
    assert verdict in (Verdict.NEWER_COMPATIBLE, Verdict.CURRENT)


async def test_upgrade_downgrade_and_re_upgrade(scratch):
    """The revision round-trips and its downgrade keeps the columns."""
    await mig.upgrade(scratch, "0015_projects")
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")

    async with mig.connected(scratch) as conn:
        cols = await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name='users' AND column_name IN "
            "('hard_delete_claimed_at','hard_delete_claim_recovered')"
        )
    assert {c["column_name"] for c in cols} == {
        "hard_delete_claimed_at",
        "hard_delete_claim_recovered",
    }

    await asyncio.to_thread(command.downgrade, alembic_config(mig.url(scratch)), "0015_projects")
    async with mig.connected(scratch) as conn:
        kept = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='users' AND column_name IN "
            "('hard_delete_claimed_at','hard_delete_claim_recovered')"
        )
        version = await conn.fetchval("SELECT version_num FROM hybridinference_alembic_version")
    # The downgrade is expand-only: the columns stay, so older code still runs.
    assert {c["column_name"] for c in kept} == {
        "hard_delete_claimed_at",
        "hard_delete_claim_recovered",
    }
    assert version == "0015_projects"

    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    async with mig.connected(scratch) as conn:
        assert await conn.fetchval("SELECT version_num FROM hybridinference_alembic_version") == (
            "0016_hard_delete_claim_lease"
        )


async def test_earlier_code_runs_on_a_0016_database(scratch):
    """The old code's queries still work on the new schema.

    This is the mixed-version case: a gateway running the previous release
    against a database that has been migrated to 0016.
    """
    await mig.upgrade(scratch, "0016_hard_delete_claim_lease")
    async with mig.connected(scratch) as conn:
        # The queries the previous release's hard-delete path used.
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
